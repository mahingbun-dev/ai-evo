"""
OSS 媒体文件下载、解析、Redis 缓存服务

提供对 OSS 存储的媒体文件的下载、解析和缓存功能。
"""
import re
import os
import tempfile
import logging
from typing import Optional, Dict, Any
import requests
from app.services.redis_stop_signal import get_redis_client

logger = logging.getLogger(__name__)

# 常量定义
REDIS_KEY_PREFIX = 'oss_media_cache:'
REDIS_LOCK_PREFIX = 'oss_media_lock:'


def get_oss_url_pattern_str() -> str:
    """
    OSS 下载 URL 校验正则（基于 Settings.OSS_BASE_URL 动态构造）
    未配置 OSS_BASE_URL 时返回空串，表示 OSS 媒体功能不可用
    """
    from app.core.config import get_settings
    base = (get_settings().OSS_BASE_URL or '').strip().rstrip('/')
    if not base:
        return ''
    return rf'^https?://{re.escape(base)}/api-oss/public/oss/file/hashDownload/.+\.\w+$'


class OSSMediaCacheService:
    """OSS 媒体文件下载、解析、Redis 缓存服务"""

    def __init__(self, redis_client=None, auth_service=None):
        """
        初始化 OSS 媒体缓存服务

        Args:
            redis_client: Redis 客户端实例，如果为 None 则自动获取
            auth_service: 预留的认证服务实例（当前实现走 OSS_COOKIE 配置通道）
        """
        self.redis = redis_client or get_redis_client()
        self.auth_service = auth_service

    def validate_oss_url(self, url: str) -> bool:
        """
        校验是否为合法的 OSS URL

        Args:
            url: 待校验的 URL 字符串

        Returns:
            True 如果 URL 合法，否则 False
        """
        pattern = get_oss_url_pattern_str()
        if not pattern or not url or not isinstance(url, str):
            return False
        return bool(re.match(pattern, url.strip()))

    def get_media_content(self, oss_url: str) -> Optional[str]:
        """
        获取媒体内容（优先缓存）

        流程：
        1. 校验 URL 格式，不合法返回 None
        2. 查 Redis 缓存（key = REDIS_KEY_PREFIX + oss_url）
        3. 缓存命中 → 直接返回
        4. 缓存未命中 → 获取分布式锁 → download_from_oss → parse_file_content → 存缓存 → 返回

        Args:
            oss_url: OSS 文件的 download URL

        Returns:
            解析后的文本内容，如果获取失败返回 None
        """
        # 1. 校验 URL 格式
        if not self.validate_oss_url(oss_url):
            logger.warning(f"无效的 OSS URL: {oss_url}")
            return None

        oss_url = oss_url.strip()

        # 2. 查 Redis 缓存
        cache_key = f"{REDIS_KEY_PREFIX}{oss_url}"
        cached_content = self.redis.get(cache_key)
        if cached_content:
            return cached_content

        # 3. 缓存未命中，获取分布式锁防止缓存击穿
        lock_key = f"{REDIS_LOCK_PREFIX}{oss_url}"
        lock_acquired = False
        try:
            # 尝试获取锁，60秒超时
            lock_acquired = self.redis.set(lock_key, "1", nx=True, ex=60)

            if not lock_acquired:
                # 未获取到锁，等待一段时间后重试读取缓存
                import time
                for _ in range(10):  # 最多等待 5 秒
                    time.sleep(0.5)
                    cached_content = self.redis.get(cache_key)
                    if cached_content:
                        logger.info(f"等待后从缓存获取媒体内容: {oss_url[:50]}...")
                        return cached_content
                # 等待后仍未获取到缓存，执行下载
                logger.warning(f"等待缓存超时，执行下载: {oss_url[:50]}...")

            # 4. 下载并解析文件
            file_bytes = self.download_from_oss(oss_url)
            if not file_bytes:
                logger.error(f"下载 OSS 文件失败: {oss_url}")
                return None

            # 提取文件名
            filename = oss_url.split('/')[-1]
            parsed_content = self.parse_file_content(file_bytes, filename)

            if parsed_content:
                # 5. 存缓存（永不过期）
                self.redis.set(cache_key, parsed_content)

            return parsed_content

        except Exception as e:
            logger.error(f"获取媒体内容失败: {oss_url}, 错误: {str(e)}")
            return None
        finally:
            # 释放锁
            if lock_acquired:
                try:
                    self.redis.delete(lock_key)
                except Exception as e:
                    logger.warning(f"释放锁失败: {lock_key}, 错误: {str(e)}")

    def download_from_oss(self, oss_url: str) -> Optional[bytes]:
        """
        从 OSS 下载文件

        使用 GET 请求下载文件，携带配置的认证 Cookie。

        Args:
            oss_url: OSS 文件的 download URL

        Returns:
            文件内容的 bytes，如果下载失败返回 None
        """
        try:
            # 获取认证 Cookie
            cookie = self._get_auth_cookie()

            # 发起下载请求
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            response = requests.get(oss_url, cookies=cookie, headers=headers, timeout=60)

            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"下载 OSS 文件失败，HTTP 状态码: {response.status_code}, URL: {oss_url}")
                return None

        except requests.RequestException as e:
            logger.error(f"下载 OSS 文件请求失败: {oss_url}, 错误: {str(e)}")
            return None

    def _get_auth_cookie(self) -> Dict[str, str]:
        """
        获取认证 Cookie

        优先从配置中获取 OSS_COOKIE，如果没有配置则返回空字典。

        Returns:
            Cookie 字典
        """
        try:
            from app.core.config import get_settings
            settings = get_settings()
            oss_cookie = getattr(settings, 'OSS_COOKIE', None)

            if oss_cookie:
                # 解析 Cookie 字符串为字典
                cookie_dict = {}
                if oss_cookie and isinstance(oss_cookie, str):
                    cookie_pairs = oss_cookie.split(';')
                    for pair in cookie_pairs:
                        if '=' in pair:
                            key, value = pair.strip().split('=', 1)
                            cookie_dict[key] = value
                return cookie_dict

        except Exception as e:
            logger.warning(f"获取 OSS Cookie 失败: {str(e)}")

        return {}

    def parse_file_content(self, file_bytes: bytes, filename: str) -> Optional[str]:
        """
        解析文件内容

        将 bytes 写入临时文件，根据文件扩展名调用 MediaProcessor 的对应方法进行解析。

        Args:
            file_bytes: 文件内容的 bytes
            filename: 文件名（包含扩展名）

        Returns:
            解析后的文本内容，解析失败返回 None
        """
        # 写入临时文件
        ext = os.path.splitext(filename)[1].lower()

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            try:
                # 根据文件类型调用对应的解析方法
                if ext in ['.xlsx', '.xls']:
                    return self._convert_excel_to_markdown(tmp_path)
                elif ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                    return self._describe_image(tmp_path)
                elif ext == '.pdf':
                    return self._extract_pdf_text(tmp_path)
                else:
                    logger.warning(f"不支持的文件类型: {ext}")
                    # 尝试作为文本返回
                    try:
                        return file_bytes.decode('utf-8')
                    except:
                        return f"[不支持的文件类型: {ext}]"

            finally:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception as e:
                        logger.warning(f"清理临时文件失败: {tmp_path}, 错误: {str(e)}")

        except Exception as e:
            logger.error(f"解析文件失败: {filename}, 错误: {str(e)}")
            return None

    def _convert_excel_to_markdown(self, file_path: str) -> str:
        """将 Excel 文件转换为 Markdown 表格"""
        try:
            import pandas as pd
            df = pd.read_excel(file_path, sheet_name=0)
            df.columns = df.columns.astype(str)
            return df.to_markdown(index=False)
        except Exception as e:
            return f"[Excel 读取失败: {str(e)}]"

    def _describe_image(self, file_path: str) -> str:
        """获取图片描述"""
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                width, height = img.size
                format_str = img.format
            return f"[图片: {format_str}, 尺寸 {width}x{height}]"
        except Exception as e:
            return f"[图片读取失败: {str(e)}]"

    def _extract_pdf_text(self, file_path: str) -> str:
        """提取 PDF 文本内容"""
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                text_parts = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
            return "\n".join(text_parts)
        except ImportError:
            return "[PDF 处理需要安装 pdfplumber 库]"
        except Exception as e:
            return f"[PDF 读取失败: {str(e)}]"

    def invalidate_cache(self, oss_url: str) -> bool:
        """
        删除指定 URL 的缓存

        Args:
            oss_url: OSS 文件的 download URL

        Returns:
            True 如果删除成功或 key 不存在，False 如果删除失败
        """
        try:
            oss_url = oss_url.strip()
            cache_key = f"{REDIS_KEY_PREFIX}{oss_url}"
            result = self.redis.delete(cache_key)
            logger.info(f"删除缓存: {cache_key}, 结果: {result}")
            return True
        except Exception as e:
            logger.error(f"删除缓存失败: {oss_url}, 错误: {str(e)}")
            return False

    def invalidate_dataset_cache(self, dataset_id: str, db) -> int:
        """
        删除数据集所有媒体文件的缓存

        Args:
            dataset_id: 数据集 ID
            db: 数据库会话

        Returns:
            清除的缓存数量
        """
        from app.models.dataset_models import EvaluationData, EvaluationDataV2

        try:
            cleared_count = 0

            # 处理 V1 数据
            data_items_v1 = db.query(EvaluationData).filter(
                EvaluationData.dataset_id == dataset_id,
                EvaluationData.is_active == True
            ).all()

            for item in data_items_v1:
                meta = item.meta_data or {}
                if not isinstance(meta, dict):
                    continue

                # 检查 user_input_is_media
                user_input_is_media = meta.get('user_input_is_media', False)
                if user_input_is_media in [True, 'true', 'True', '1', 1]:
                    question = item.question
                    if question and self.validate_oss_url(question):
                        if self.invalidate_cache(question):
                            cleared_count += 1

                # 检查 answer_is_media
                answer_is_media = meta.get('answer_is_media', False)
                if answer_is_media in [True, 'true', 'True', '1', 1]:
                    ref_answer = item.ref_answer
                    if ref_answer and self.validate_oss_url(ref_answer):
                        if self.invalidate_cache(ref_answer):
                            cleared_count += 1

            # 处理 V2 数据
            data_items_v2 = db.query(EvaluationDataV2).filter(
                EvaluationDataV2.dataset_id == dataset_id,
                EvaluationDataV2.is_active == True
            ).all()

            for item in data_items_v2:
                data = item.data or {}
                if not isinstance(data, dict):
                    continue

                # V2 格式中，is_media 标志直接在 data JSON 中
                for field_name, field_value in data.items():
                    if field_name.endswith("_is_media"):
                        continue  # 跳过 is_media 标志字段本身
                    is_media_key = f"{field_name}_is_media"
                    is_media = data.get(is_media_key, False)
                    if is_media in [True, 'true', 'True', '1', 1]:
                        if field_value and self.validate_oss_url(str(field_value)):
                            if self.invalidate_cache(str(field_value)):
                                cleared_count += 1

            logger.info(f"已清除数据集 {dataset_id} 的 {cleared_count} 个媒体缓存")
            return cleared_count

        except Exception as e:
            logger.error(f"清除数据集缓存失败: {dataset_id}, 错误: {str(e)}")
            return 0
