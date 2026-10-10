"""
媒体文件处理服务

支持处理：
- Excel文件 (.xlsx, .xls) -> Markdown表格
- 图片文件 (.jpg, .jpeg, .png, .gif) -> Base64或文本描述
- PDF文件 (.pdf) -> Markdown文本
- 音频文件 (.mp3, .wav, .m4a) -> 文本转写（预留接口）
"""
import os
import re
import base64
import json
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import pandas as pd
from PIL import Image


class MediaProcessor:
    """媒体文件处理器"""

    def __init__(self, db=None, base_path=None):
        self.db = db
        # 默认使用当前目录下的 uploads 文件夹
        self.base_path = base_path or os.path.join(os.getcwd(), "uploads")

    def process_content(self, content: str) -> str:
        """
        处理内容中的媒体文件引用

        Args:
            content: 原始内容，可能包含媒体文件引用

        Returns:
            处理后的内容，媒体文件已转换为文本描述
        """
        if not content:
            return content

        # 处理Excel文件引用
        content = self._process_excel_reference(content)

        # 处理图片文件引用
        content = self._process_image_reference(content)

        # 处理PDF文件引用
        content = self._process_pdf_reference(content)

        return content

    def _process_excel_reference(self, content: str) -> str:
        """处理Excel文件引用，转换为Markdown表格"""
        # 匹配Excel文件路径或URL，支持包含空格和括号的Windows路径
        # 匹配以 .xls 或 .xlsx 结尾的完整路径
        excel_pattern = r'[^"\n]*\.(?:xlsx|xls)'

        matches = re.findall(excel_pattern, content)
        for match in matches:
            try:
                markdown = self._convert_excel_to_markdown(match)
                content = content.replace(match, f"\n{markdown}\n")
            except Exception as e:
                content = content.replace(match, f"[Excel文件处理失败: {str(e)}]")

        return content

    def _process_image_reference(self, content: str) -> str:
        """处理图片文件引用"""
        # 匹配图片文件路径或URL，支持包含空格和括号的路径
        image_pattern = r'[^"\n]*\.(?:jpg|jpeg|png|gif|webp)'

        matches = re.findall(image_pattern, content)
        for match in matches:
            try:
                image_desc = self._describe_image(match)
                content = content.replace(match, f"\n[图片描述: {image_desc}]\n")
            except Exception as e:
                content = content.replace(match, f"[图片处理失败: {str(e)}]")

        return content

    def _process_pdf_reference(self, content: str) -> str:
        """处理PDF文件引用"""
        # 匹配PDF文件路径或URL，支持包含空格和括号的路径
        pdf_pattern = r'[^"\n]*\.pdf'

        matches = re.findall(pdf_pattern, content)
        for match in matches:
            try:
                text = self._extract_pdf_text(match)
                content = content.replace(match, f"\n[PDF内容: {text[:1000]}...]\n")
            except Exception as e:
                content = content.replace(match, f"[PDF处理失败: {str(e)}]")

        return content

    def _convert_excel_to_markdown(self, file_path: str) -> str:
        """将Excel文件转换为Markdown表格"""
        # 清理路径：去除首尾空格和引号
        file_path = file_path.strip().strip('"').strip("'")

        # 如果是URL或远程文件，需要先下载
        if file_path.startswith('http://') or file_path.startswith('https://'):
            # TODO: 实现远程文件下载
            return f"Excel文件: {file_path}"

        # 本地文件处理
        path = Path(file_path)
        if not path.exists():
            # 尝试使用 base_path 拼接完整路径
            full_path = Path(self.base_path) / file_path
            if full_path.exists():
                path = full_path
                file_path = str(full_path)
            else:
                return f"[Excel文件不存在: {file_path}]"

        try:
            # 读取Excel（只读取第一个sheet）
            df = pd.read_excel(file_path, sheet_name=0)

            # 处理列名
            df.columns = df.columns.astype(str)

            # 转换为Markdown表格
            markdown = df.to_markdown(index=False)
            return markdown

        except Exception as e:
            return f"[Excel读取失败: {str(e)}]"

    def _describe_image(self, file_path: str) -> str:
        """获取图片描述"""
        # 清理路径：去除首尾空格和引号
        file_path = file_path.strip().strip('"').strip("'")

        if file_path.startswith('http://') or file_path.startswith('https://'):
            # TODO: 实现远程图片下载和OCR
            return f"[图片: {file_path}]"

        path = Path(file_path)
        if not path.exists():
            # 尝试使用 base_path 拼接完整路径
            full_path = Path(self.base_path) / file_path
            if full_path.exists():
                path = full_path
                file_path = str(full_path)
            else:
                return f"[图片不存在: {file_path}]"

        try:
            # 获取图片基本信息
            with Image.open(file_path) as img:
                width, height = img.size
                format_str = img.format

            return f"[图片: {format_str}, 尺寸 {width}x{height}]"

        except Exception as e:
            return f"[图片读取失败: {str(e)}]"

    def image_to_base64(self, file_path: str) -> str:
        """将图片转换为Base64编码"""
        path = Path(file_path)
        if not path.exists():
            return f"图片文件不存在: {file_path}"

        with open(file_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

    def _extract_pdf_text(self, file_path: str) -> str:
        """提取PDF文本内容"""
        try:
            import pdfplumber
        except ImportError:
            return "[PDF处理需要安装pdfplumber库]"

        if file_path.startswith('http://') or file_path.startswith('https://'):
            return f"[PDF文件: {file_path}]"

        path = Path(file_path)
        if not path.exists():
            return f"[PDF文件不存在: {file_path}]"

        try:
            with pdfplumber.open(file_path) as pdf:
                text_parts = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)

                return "\n".join(text_parts)

        except Exception as e:
            return f"[PDF读取失败: {str(e)}]"
