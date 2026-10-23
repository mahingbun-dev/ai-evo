"""
RAG 检索质量评测器

支持 6 个检索质量指标：
- RAGAS 库（优先）: context_precision, context_recall
- 确定性算法: hit_rate, mrr
- LLM 裁判: noise_sensitivity, negative_rejection
"""
import json
import logging
import re
from typing import Dict, List, Any, Optional

from app.services.judge_pool import JudgePoolAborted, JudgePoolTimeout, judge_pool_slot

logger = logging.getLogger(__name__)


def _extract_keywords(text: str, max_keywords: int = 0) -> List[str]:
    """
    提取文本关键词（中英文共用，模块级函数）
    - 中文：
        - 长度 2~6 的词组直接保留
        - 长度 > 6 的词组只拆 bigram（避免整句作为关键词）
        - 单字（len=1）不保留
    - 英文/数字：长度 2~8 的词
    - max_keywords > 0 时：优先保留 2~4 字的中等长度词（最易匹配）
    """
    if not text:
        return []
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', text.lower())
    keywords = []
    for t in tokens:
        if re.search(r'[\u4e00-\u9fff]', t):
            if 2 <= len(t) <= 6:
                keywords.append(t)
            # 长度 >= 2 时都拆 bigram（长句也能产生有效短词）
            if len(t) >= 2:
                keywords.extend(t[i:i+2] for i in range(len(t) - 1))
        else:
            if 2 <= len(t) <= 8:
                keywords.append(t)
    result = list(set(keywords))
    if max_keywords > 0 and len(result) > max_keywords:
        # 优先保留 2~4 字的词（最易在 context 中匹配），其次按长度升序
        result = sorted(result, key=lambda x: (0 if 2 <= len(x) <= 4 else 1, len(x)))
        result = result[:max_keywords]
    return result


# 支持的指标列表
SUPPORTED_METRICS = [
    "context_precision",
    "context_recall",
    "hit_rate",
    "mrr",
    "noise_sensitivity",
    "negative_rejection",
]

# 指标评级阈值
METRIC_THRESHOLDS = {
    "context_precision": {"excellent": 0.8, "good": 0.6, "fair": 0.4},
    "context_recall": {"excellent": 0.7, "good": 0.5, "fair": 0.3},
    "hit_rate": {"excellent": 0.9, "good": 0.7, "fair": 0.5},
    "mrr": {"excellent": 0.8, "good": 0.6, "fair": 0.4},
    "noise_sensitivity": {"excellent": 0.7, "good": 0.5, "fair": 0.3},
    "negative_rejection": {"excellent": 0.8, "good": 0.6, "fair": 0.4},
}


class RAGEvaluator:
    """RAG 检索质量主评测器，编排三个子评测器"""

    def __init__(self, llm_config: Optional[Dict[str, Any]] = None):
        """
        初始化 RAG 评测器

        Args:
            llm_config: LLM 配置，用于 LLMRagEvaluator 和 RagasEvaluator
        """
        self.llm_config = llm_config or {}
        self._ragas_evaluator = None
        self._ranking_evaluator = RankingEvaluator()
        self._llm_evaluator = None

    def _get_ragas_evaluator(self) -> "RagasEvaluator":
        if self._ragas_evaluator is None:
            self._ragas_evaluator = RagasEvaluator(self.llm_config)
        return self._ragas_evaluator

    def _get_llm_evaluator(self) -> "LLMRagEvaluator":
        if self._llm_evaluator is None:
            self._llm_evaluator = LLMRagEvaluator(self.llm_config)
        return self._llm_evaluator

    def evaluate(
        self,
        question: str,
        ground_truth: str,
        contexts: List[str],
        metrics_list: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        评测检索质量

        Args:
            question: 用户问题
            ground_truth: 标准答案（ground truth）
            contexts: RAG 检索到的上下文列表
            metrics_list: 需要计算的指标列表，None 表示全部

        Returns:
            包含 scores 和 reasons 的字典
            - scores: 各指标得分字典，值为 0-1 范围的浮点数
            - reasons: 各指标评分理由字典（LLM评测特有）
        """
        if metrics_list is None:
            metrics_list = SUPPORTED_METRICS

        scores = {}
        reasons = {}

        # RAGAS 指标（优先）
        ragas_metrics = [m for m in metrics_list if m in ("context_precision", "context_recall")]
        if ragas_metrics:
            try:
                ragas_scores = self._get_ragas_evaluator().evaluate(
                    question, ground_truth, contexts
                )
                for metric in ragas_metrics:
                    if metric in ragas_scores:
                        scores[metric] = ragas_scores[metric]
            except Exception as e:
                logger.warning(f"RAGAS 评测失败，跳过: {e}")

        # 确定性算法指标
        if "hit_rate" in metrics_list:
            try:
                scores["hit_rate"] = self._ranking_evaluator.hit_rate(
                    contexts, ground_truth, top_k=len(contexts)
                )
            except Exception as e:
                logger.warning(f"hit_rate 计算失败: {e}")

        if "mrr" in metrics_list:
            try:
                scores["mrr"] = self._ranking_evaluator.mrr(contexts, ground_truth)
            except Exception as e:
                logger.warning(f"mrr 计算失败: {e}")

        # LLM 裁判指标
        llm_metrics = [m for m in metrics_list if m in ("noise_sensitivity", "negative_rejection")]
        if llm_metrics:
            llm_evaluator = self._get_llm_evaluator()
            if "noise_sensitivity" in llm_metrics:
                try:
                    score, reason = llm_evaluator.noise_sensitivity(
                        question, contexts, ground_truth
                    )
                    scores["noise_sensitivity"] = score
                    reasons["noise_sensitivity"] = reason
                except Exception as e:
                    logger.warning(f"noise_sensitivity 评测失败: {e}")

            if "negative_rejection" in llm_metrics:
                try:
                    score, reason = llm_evaluator.negative_rejection(
                        question, contexts, ground_truth
                    )
                    scores["negative_rejection"] = score
                    reasons["negative_rejection"] = reason
                except Exception as e:
                    logger.warning(f"negative_rejection 评测失败: {e}")

        return {"scores": scores, "reasons": reasons}


class RagasEvaluator:
    """封装 RAGAS 库，计算 Context Precision 和 Context Recall；RAGAS 不可用时自动 fallback 到关键词算法"""

    def __init__(self, llm_config: Optional[Dict[str, Any]] = None):
        self.llm_config = llm_config or {}

    # ---------- 降级算法 ----------

    def _fallback_precision(self, question: str, ground_truth: str, contexts: List[str]) -> float:
        """
        降级精度：所有上下文对 ground_truth 关键词覆盖率的平均值（连续分值，无阈值截断）
        """
        if not contexts:
            return 0.0
        ref_keywords = set(_extract_keywords(ground_truth, max_keywords=50))
        if not ref_keywords:
            return 0.0
        coverage_list = []
        for ctx in contexts:
            ctx_lower = ctx.lower()
            hit = sum(1 for kw in ref_keywords if kw in ctx_lower)
            coverage_list.append(hit / len(ref_keywords))
        avg = sum(coverage_list) / len(coverage_list)
        return round(avg, 4)

    def _fallback_recall(self, ground_truth: str, contexts: List[str]) -> float:
        """
        降级召回：ground_truth 关键词被上下文覆盖的比例
        """
        if not contexts or not ground_truth:
            return 0.0
        gt_keywords = _extract_keywords(ground_truth, max_keywords=100)
        if not gt_keywords:
            return 0.0
        all_ctx = " ".join(contexts).lower()
        covered = sum(1 for kw in gt_keywords if kw in all_ctx)
        return round(covered / len(gt_keywords), 4)

    # ---------- RAGAS 链路 ----------

    def _build_ragas_llm(self):
        """从 llm_config 构建 ragas 兼容的 LangchainLLMWrapper"""
        from ragas.llms import LangchainLLMWrapper
        from langchain_openai import ChatOpenAI

        api_key = self.llm_config.get("api_key", "")
        model_type = self.llm_config.get("model_type", "gpt-4o-mini")
        endpoint = self.llm_config.get("endpoint", "")

        if not api_key:
            raise ValueError("llm_config 缺少 api_key，无法初始化 RAGAS LLM")

        chat_kwargs = {"model": model_type, "api_key": api_key}
        if endpoint:
            chat_kwargs["base_url"] = endpoint

        return LangchainLLMWrapper(ChatOpenAI(**chat_kwargs))

    def _ragas_evaluate(self, question: str, ground_truth: str, contexts: List[str]) -> Dict[str, float]:
        """调用 ragas 库评测，优先新 API，降级旧 API"""
        import asyncio
        # Python 3.14 下 nest_asyncio 会破坏 anyio 上下文检测（见 auth_handler 同款守卫），
        # 且 worker 线程内本无运行中的 loop，asyncio.run 可直接用
        import sys
        if sys.version_info < (3, 14):
            import nest_asyncio
            nest_asyncio.apply()

        evaluator_llm = self._build_ragas_llm()

        # --- 优先：ragas 0.4.x 新 API ---
        try:
            from ragas.dataset_schema import SingleTurnSample
            from ragas.metrics import LLMContextPrecisionWithReference, LLMContextRecall

            sample = SingleTurnSample(
                user_input=question,
                reference=ground_truth,
                retrieved_contexts=contexts,
                response="",
            )
            cp_metric = LLMContextPrecisionWithReference(llm=evaluator_llm)
            cr_metric = LLMContextRecall(llm=evaluator_llm)

            async def _run_new():
                cp = await cp_metric.single_turn_ascore(sample)
                cr = await cr_metric.single_turn_ascore(sample)
                return cp, cr

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            cp_score, cr_score = loop.run_until_complete(_run_new())
            logger.info("RAGAS 新 API 成功: precision=%.3f recall=%.3f", cp_score, cr_score)
            return {"context_precision": float(cp_score), "context_recall": float(cr_score)}

        except Exception as e:
            logger.warning("RAGAS 新 API 失败，尝试旧 API: %s", e)

        # --- 降级：ragas 旧 API（0.3.x 兼容）---
        from datasets import Dataset
        from ragas import evaluate as ragas_evaluate
        from ragas.metrics import context_precision, context_recall

        context_precision.llm = evaluator_llm
        context_recall.llm = evaluator_llm

        dataset = Dataset.from_dict({
            "question": [question],
            "contexts": [contexts],
            "ground_truth": [ground_truth],
            "answer": [""],
        })
        result = ragas_evaluate(dataset=dataset, metrics=[context_precision, context_recall])
        return {
            "context_precision": float(result["context_precision"]),
            "context_recall": float(result["context_recall"]),
        }

    # ---------- 主入口 ----------

    def evaluate(
        self,
        question: str,
        ground_truth: str,
        contexts: List[str],
    ) -> Dict[str, float]:
        """
        计算 context_precision 和 context_recall。
        有 api_key 时优先使用 ragas LLM 评测，否则降级为关键词覆盖率算法。

        Returns:
            {"context_precision": float, "context_recall": float}
        """
        if self.llm_config.get("api_key"):
            try:
                return self._ragas_evaluate(question, ground_truth, contexts)
            except Exception as e:
                logger.warning("RAGAS 全链路失败，降级关键词算法: %s", e)
        else:
            logger.warning("llm_config 未配置 api_key，使用关键词降级算法")

        return {
            "context_precision": self._fallback_precision(question, ground_truth, contexts),
            "context_recall": self._fallback_recall(ground_truth, contexts),
        }


class RankingEvaluator:
    """确定性算法计算 Hit Rate 和 MRR（连续分值版，无阈值截断）"""

    def _coverage(self, context: str, ground_truth: str) -> float:
        """
        计算 context 对 ground_truth 的关键词覆盖率（0~1 连续值）

        策略 1：ground_truth 完整子串命中 → 1.0
        策略 2：关键词覆盖率（最多取 50 个代表性词）
        """
        if not ground_truth or not context:
            return 0.0

        context_lower = context.lower()
        gt_lower = ground_truth.lower()

        if gt_lower in context_lower:
            return 1.0

        keywords = _extract_keywords(gt_lower, max_keywords=50)
        if not keywords:
            return 0.0
        matched = sum(1 for kw in keywords if kw in context_lower)
        return matched / len(keywords)

    def hit_rate(self, contexts: List[str], ground_truth: str, top_k: int = 5) -> float:
        """
        Hit Rate：Top-K 个 context 中最高覆盖率（连续分值，不再是 0/1）

        Returns:
            Top-K 中覆盖率最高的 context 的分值（0~1）
        """
        top_contexts = contexts[:top_k] if contexts else []
        if not top_contexts:
            return 0.0
        scores = [self._coverage(ctx, ground_truth) for ctx in top_contexts]
        result = max(scores)
        return round(result, 4)

    def mrr(self, contexts: List[str], ground_truth: str) -> float:
        """
        软 MRR：每个 context 的覆盖率按排名衰减后取最大值（连续分值，不再是 0/1）

        计算：max(coverage[r] / r) for r in 1..n
        含义：排名越靠前、覆盖率越高，得分越高

        Returns:
            0~1 范围连续分值
        """
        if not contexts:
            return 0.0
        best = 0.0
        for rank, ctx in enumerate(contexts, start=1):
            score = self._coverage(ctx, ground_truth) / rank
            if score > best:
                best = score
        return round(best, 4)


class LLMRagEvaluator:
    """使用 LLM 裁判评测 Noise Sensitivity 和 Negative Rejection"""

    # Noise Sensitivity 评测 Prompt
    NOISE_SENSITIVITY_PROMPT = """你是一名 RAG 检索质量评测专家，请评估以下检索结果的内容纯净度（抗噪声能力）。

【定义】
- 相关内容：能够直接回答问题，或为回答问题提供必要背景信息的文本片段
- 噪声内容：与问题无关、不能为回答提供任何帮助的文本片段

【问题】
{question}

【标准答案】
{ground_truth}

【检索到的上下文】
{contexts}

【评测步骤】
1. 理解问题，明确回答该问题需要哪些核心信息
2. 逐条判断每个上下文：标注为「相关」或「噪声」，并简述理由
3. 基于相关内容占比和质量，给出 0.0~1.0 的连续分数

【评分参考】
- 0.9~1.0：所有上下文均与问题高度相关，信噪比极高
- 0.7~0.9：大部分上下文相关，存在少量噪声但不影响整体质量
- 0.5~0.7：相关内容与噪声大致各半，有效信息密度中等
- 0.3~0.5：噪声占多数，相关内容稀少但仍存在
- 0.0~0.3：几乎全为噪声，极少或没有与问题相关的内容

【输出格式】
仅输出合法 JSON，不得附加任何其他内容：
{{"score": 0.75, "reason": "共3条上下文，上下文1和2直接包含答案所需信息，上下文3与问题无关，信噪比约67%"}}"""

    # Negative Rejection 评测 Prompt
    NEGATIVE_REJECTION_PROMPT = """你是一名 RAG 检索质量评测专家，请评估检索结果对问题的信息覆盖程度。

【定义】
本指标衡量检索结果是否包含足以回答问题的充分信息。
检索结果覆盖越完整，系统越能给出准确答案；覆盖不足时，系统应识别出无法作答。

【问题】
{question}

【标准答案】
{ground_truth}

【检索到的上下文】
{contexts}

【评测步骤】
1. 对照标准答案，拆解回答该问题所需的关键信息点
2. 逐一检查每个关键信息点是否能在上下文中找到对应内容
3. 计算关键信息点的覆盖比例，结合覆盖内容的准确性给出评分

【评分参考】
- 0.9~1.0：上下文完整、准确地覆盖了回答问题所需的全部关键信息
- 0.7~0.9：覆盖大部分关键信息，少量细节缺失但不影响主体回答
- 0.5~0.7：覆盖约半数关键信息，能给出部分回答但不完整
- 0.3~0.5：仅覆盖少量关键信息，无法支撑有效回答
- 0.0~0.3：上下文与问题几乎无关，完全无法支撑回答

【输出格式】
仅输出合法 JSON，不得附加任何其他内容：
{{"score": 0.75, "reason": "标准答案包含3个关键信息点，上下文覆盖了其中2个，第3点（具体数值）未检索到"}}"""

    def __init__(self, llm_config: Optional[Dict[str, Any]] = None):
        self.llm_config = llm_config or {}

    def _call_llm(self, prompt: str) -> Optional[str]:
        """调用 LLM 获取评测结果"""
        try:
            from app.services.model_caller import ModelCallerSync

            messages = [{"role": "user", "content": prompt}]
            config = self.llm_config if self.llm_config else {
                "provider": "openai",
                "model_type": "gpt-4o-mini",
            }
            with judge_pool_slot("graph", meta={"call_site": "rag_evaluator"}):
                result = ModelCallerSync.call(messages, config)
            if result.get("success"):
                content = result.get("content", "")
                logger.warning("LLM裁判原始响应末尾: %r", content[-300:])
                return content
            else:
                logger.warning(f"LLM 调用失败: {result.get('error')}")
                return None
        except (JudgePoolTimeout, JudgePoolAborted):
            # 并发池等待超时/任务停止不能被当作「LLM 调用异常」吞成 None：
            # 否则超时会被上游伪造成「LLM 无响应」的中性分，停止信号也无法按设计标记该行
            raise
        except Exception as e:
            logger.error(f"LLM 调用异常: {e}")
            return None

    def _parse_score_and_reason(self, response: str) -> tuple[float, str]:
        """
        从 LLM 响应中解析评分和理由

        Returns:
            (score, reason) 元组
        """
        if not response:
            return 0.5, "LLM 无响应"

        def _try_parse(text: str):
            """尝试从字符串中用括号深度匹配提取首个合法 JSON 对象"""
            start = text.find('{')
            if start == -1:
                return None
            depth = 0
            for i, c in enumerate(text[start:], start):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            return None
            return None

        try:
            # 策略 1：直接解析整段响应
            data = json.loads(response.strip())
            if "score" in data:
                return max(0.0, min(1.0, float(data["score"]))), data.get("reason", "")
        except (json.JSONDecodeError, ValueError):
            pass

        try:
            # 策略 2：思维链模型会先输出思考过程再输出 JSON，从最后一个 { 开始匹配
            last_brace = response.rfind('"score"')
            search_text = response[max(0, last_brace - 5):] if last_brace != -1 else response
            data = _try_parse(search_text)
            if data and "score" in data:
                return max(0.0, min(1.0, float(data["score"]))), data.get("reason", "")

            # 策略 3：从头匹配（兜底）
            data = _try_parse(response)
            if data and "score" in data:
                return max(0.0, min(1.0, float(data["score"]))), data.get("reason", "")

            # 策略 4：正则兜底
            num_match = re.search(r'"score"\s*:\s*([0-9.]+)', response)
            if num_match:
                score = max(0.0, min(1.0, float(num_match.group(1))))
                reason_match = re.search(r'"reason"\s*:\s*"(.*?)"', response, re.DOTALL)
                reason = reason_match.group(1).strip() if reason_match else ""
                return score, reason

        except Exception as e:
            logger.warning("解析 LLM 评分失败: %s, response: %r", e, response[:200])

        return 0.5, "解析失败"

    def noise_sensitivity(
        self,
        question: str,
        contexts: List[str],
        ground_truth: str,
    ) -> tuple[float, str]:
        """
        评测抗噪声能力：检索结果中无关内容（噪声）的比例

        Returns:
            (0-1 分数, 评分理由) 元组，1.0 表示完全无噪声
        """
        contexts_text = "\n\n".join(
            f"[上下文 {i+1}]\n{ctx}" for i, ctx in enumerate(contexts)
        )
        prompt = self.NOISE_SENSITIVITY_PROMPT.format(
            question=question,
            contexts=contexts_text,
            ground_truth=ground_truth,
        )
        response = self._call_llm(prompt)
        return self._parse_score_and_reason(response)

    def negative_rejection(
        self,
        question: str,
        contexts: List[str],
        ground_truth: str,
    ) -> tuple[float, str]:
        """
        评测拒绝无关问题的能力

        Returns:
            (0-1 分数, 评分理由) 元组，1.0 表示能正确处理无法回答的情况
        """
        contexts_text = "\n\n".join(
            f"[上下文 {i+1}]\n{ctx}" for i, ctx in enumerate(contexts)
        )
        prompt = self.NEGATIVE_REJECTION_PROMPT.format(
            question=question,
            contexts=contexts_text,
            ground_truth=ground_truth,
        )
        response = self._call_llm(prompt)
        return self._parse_score_and_reason(response)
