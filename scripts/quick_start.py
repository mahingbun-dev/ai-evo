#!/usr/bin/env python3
"""
OpenCompass 快速开始脚本
提供交互式命令行界面，帮助用户快速配置和启动项目
"""

import os
import sys
import subprocess
import json
import yaml
from pathlib import Path
from typing import Dict, List, Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent

class QuickStart:
    """快速开始类"""

    def __init__(self):
        self.config_file = PROJECT_ROOT / "projects/testcase_creator/config.yaml"
        self.env_file = PROJECT_ROOT / ".env"
        self.test_data_file = PROJECT_ROOT / "projects/testcase_creator/data/e2e_testcase.json"

    def print_header(self, text: str):
        """打印标题"""
        print("\n" + "=" * 60)
        print(f"  {text}")
        print("=" * 60)

    def print_step(self, step_num: int, text: str):
        """打印步骤"""
        print(f"\n{step_num}. {text}")
        print("-" * 40)

    def check_python_version(self) -> bool:
        """检查 Python 版本"""
        version = sys.version_info
        if version.major < 3 or (version.major == 3 and version.minor < 8):
            print(f"❌ Python 版本过低: {sys.version}")
            print("   需要 Python >= 3.8")
            return False
        print(f"✅ Python 版本: {sys.version}")
        return True

    def check_project_root(self) -> bool:
        """检查是否在项目根目录"""
        if not (PROJECT_ROOT / "requirements.txt").exists():
            print(f"❌ 当前目录不是项目根目录: {PROJECT_ROOT}")
            print("   请在项目根目录运行此脚本")
            return False
        print(f"✅ 项目根目录: {PROJECT_ROOT}")
        return True

    def check_virtual_env(self) -> bool:
        """检查虚拟环境"""
        venv_path = PROJECT_ROOT / "venv"
        if venv_path.exists():
            print(f"✅ 虚拟环境已存在: {venv_path}")
            return True
        else:
            print("⚠️  虚拟环境不存在")
            return False

    def create_virtual_env(self) -> bool:
        """创建虚拟环境"""
        print("正在创建虚拟环境...")
        try:
            subprocess.run([sys.executable, "-m", "venv", "venv"],
                         cwd=PROJECT_ROOT, check=True)
            print("✅ 虚拟环境创建成功")
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ 创建虚拟环境失败: {e}")
            return False

    def check_dependencies(self) -> Dict[str, bool]:
        """检查依赖"""
        dependencies = {
            "fastapi": False,
            "pytest": False,
            "sqlalchemy": False,
            "pandas": False,
        }

        try:
            import fastapi
            dependencies["fastapi"] = True
        except ImportError:
            pass

        try:
            import pytest
            dependencies["pytest"] = True
        except ImportError:
            pass

        try:
            import sqlalchemy
            dependencies["sqlalchemy"] = True
        except ImportError:
            pass

        try:
            import pandas
            dependencies["pandas"] = True
        except ImportError:
            pass

        print("依赖检查结果:")
        for dep, installed in dependencies.items():
            status = "✅" if installed else "❌"
            print(f"  {status} {dep}")

        return dependencies

    def install_dependencies(self) -> bool:
        """安装依赖"""
        print("正在安装依赖...")

        commands = [
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            [sys.executable, "-m", "pip", "install", "-e", "pytest_core/"],
            [sys.executable, "-m", "pip", "install", "-r", "agent/requirements.txt"],
            [sys.executable, "-m", "pip", "install", "-r", "pytest_core/requirements.txt"],
        ]

        for cmd in commands:
            try:
                print(f"  执行: {' '.join(cmd)}")
                subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)
            except subprocess.CalledProcessError as e:
                print(f"❌ 安装失败: {e}")
                return False

        print("✅ 所有依赖安装成功")
        return True

    def check_config_file(self) -> bool:
        """检查配置文件"""
        if self.config_file.exists():
            print(f"✅ 配置文件存在: {self.config_file}")
            return True
        else:
            print(f"❌ 配置文件不存在: {self.config_file}")
            return False

    def check_database_config(self) -> bool:
        """检查数据库配置"""
        db_url = os.getenv("DATABASE_URL")
        if db_url:
            print(f"✅ 数据库配置已设置")
            return True
        else:
            print("⚠️  数据库配置未设置 (DATABASE_URL)")
            print("   请设置环境变量:")
            print('   export DATABASE_URL="postgresql://username:password@host:port/database"')
            return False

    def create_directories(self) -> bool:
        """创建必要目录"""
        directories = [
            PROJECT_ROOT / "projects/testcase_creator/cache",
            PROJECT_ROOT / "projects/testcase_creator/data/history_testcases",
            PROJECT_ROOT / "allure-results",
        ]

        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                print(f"✅ 创建目录: {directory}")
            except Exception as e:
                print(f"❌ 创建目录失败 {directory}: {e}")
                return False

        return True

    def check_test_data(self) -> bool:
        """检查测试数据文件"""
        if self.test_data_file.exists():
            print(f"✅ 测试数据文件存在: {self.test_data_file}")
            return True
        else:
            print(f"⚠️  测试数据文件不存在: {self.test_data_file}")
            print("   请创建测试数据文件或使用示例文件")
            return False

    def create_env_file(self) -> bool:
        """创建环境变量文件"""
        if self.env_file.exists():
            print(f"✅ 环境变量文件已存在: {self.env_file}")
            return True

        env_content = """# 数据库配置
DATABASE_URL=postgresql://username:password@localhost:5432/opencompass

# API 配置
API_KEY=your_api_key
LLM_MODEL=mimo-v2-flash
LLM_API_URL=https://api.xiaomimimo.com/v1

# Confluence 配置
CF_URL=https://confluence.example.com
CF_USERNAME=your_username
CF_PASSWORD=your_password

# SSO 配置
MEDIA_LOGIN_URL=/api/users/login
MEDIA_LOGIN_USER=your_username
MEDIA_LOGIN_PASSWORD=your_password

# 日志配置
LOG_LEVEL=INFO
LOG_FORMAT=%(asctime)s - %(name)s - %(levelname)s - %(message)s
"""

        try:
            Path(self.env_file).write_text(env_content, encoding="utf-8")
            print(f"✅ 环境变量文件已创建: {self.env_file}")
            print("   ⚠️  请编辑此文件并填写正确的配置")
            return True
        except Exception as e:
            print(f"❌ 创建环境变量文件失败: {e}")
            return False

    def create_sample_test_data(self) -> bool:
        """创建示例测试数据"""
        if self.test_data_file.exists():
            print(f"✅ 测试数据文件已存在: {self.test_data_file}")
            return True

        sample_data = {
            "case_id": "E2E-PONIT-001",
            "case_name": "端到端测试点测试",
            "description": "AI生成用例端到端测试",
            "reference": 80,
            "sub_cases": [
                {
                    "sub_case_id": "E2E-PONIT-001-1",
                    "case_name": "测试用例名称",
                    "user_input": "https://confluence.example.com/pages/viewpage.action?pageId=90779261",
                    "history_case_path": "历史用例文件.xls",
                    "priority": "P0",
                    "enabled": True
                }
            ]
        }

        try:
            Path(self.test_data_file).write_text(
                json.dumps(sample_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"✅ 示例测试数据已创建: {self.test_data_file}")
            return True
        except Exception as e:
            print(f"❌ 创建示例测试数据失败: {e}")
            return False

    def test_database_connection(self) -> bool:
        """测试数据库连接"""
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("⚠️  数据库连接测试跳过（未设置 DATABASE_URL）")
            return False

        try:
            from app.db.session import engine
            from sqlalchemy import inspect

            inspector = inspect(engine)
            tables = inspector.get_table_names()

            print(f"✅ 数据库连接成功")
            if tables:
                print(f"   数据库中有 {len(tables)} 个表")
            else:
                print("   ⚠️  数据库中没有表，将在启动时自动创建")

            return True
        except Exception as e:
            print(f"❌ 数据库连接失败: {e}")
            return False

    def run_simple_test(self) -> bool:
        """运行简单测试验证安装"""
        print("正在运行简单测试验证安装...")

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest",
                 "projects/testcase_creator/tests/test_e2e.py::TestE2E::test_testpoint_quality",
                 "-v", "--tb=short"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                print("✅ 测试验证成功")
                return True
            else:
                print("⚠️  测试验证失败（可能是配置问题）")
                print("   输出:", result.stdout[:500])
                print("   错误:", result.stderr[:500])
                return False

        except subprocess.TimeoutExpired:
            print("⚠️  测试验证超时")
            return False
        except Exception as e:
            print(f"❌ 运行测试失败: {e}")
            return False

    def show_summary(self):
        """显示总结"""
        self.print_header("设置完成！")

        print("\n下一步操作:")
        print("1. 编辑 .env 文件，填写正确的配置")
        print("2. 编辑 projects/testcase_creator/config.yaml 文件")
        print("3. 启动 FastAPI 服务:")
        print("   python -m app.main")
        print("4. 运行测试:")
        print("   pytest -m e2e")
        print("5. 查看详细文档:")
        print("   cat USAGE_GUIDE.md")
        print("   cat QUICK_START.md")

        print("\n常用命令:")
        print("  启动服务: python -m app.main")
        print("  运行测试: pytest -m e2e -v -s")
        print("  生成报告: pytest --alluredir=allure-results && allure serve allure-results")

        print("\n文档位置:")
        print(f"  使用说明: {PROJECT_ROOT / 'USAGE_GUIDE.md'}")
        print(f"  快速开始: {PROJECT_ROOT / 'QUICK_START.md'}")

    def run(self):
        """运行快速开始"""
        self.print_header("OpenCompass 快速开始")

        steps = [
            ("检查 Python 版本", self.check_python_version),
            ("检查项目根目录", self.check_project_root),
            ("检查虚拟环境", self.check_virtual_env),
            ("创建虚拟环境", self.create_virtual_env),
            ("检查依赖", self.check_dependencies),
            ("安装依赖", self.install_dependencies),
            ("检查配置文件", self.check_config_file),
            ("检查数据库配置", self.check_database_config),
            ("创建必要目录", self.create_directories),
            ("检查测试数据", self.check_test_data),
            ("创建环境变量文件", self.create_env_file),
            ("创建示例测试数据", self.create_sample_test_data),
            ("测试数据库连接", self.test_database_connection),
            ("运行简单测试", self.run_simple_test),
        ]

        results = []
        for i, (step_name, step_func) in enumerate(steps, 1):
            self.print_step(i, step_name)
            try:
                result = step_func()
                results.append((step_name, result))
            except Exception as e:
                print(f"❌ 步骤失败: {e}")
                results.append((step_name, False))

        # 显示结果总结
        self.print_header("执行结果总结")
        print("\n步骤执行结果:")
        for step_name, result in results:
            status = "✅" if result else "❌"
            print(f"  {status} {step_name}")

        success_count = sum(1 for _, result in results if result)
        total_count = len(results)
        print(f"\n成功: {success_count}/{total_count}")

        if success_count == total_count:
            self.show_summary()
        else:
            print("\n⚠️  部分步骤失败，请检查错误信息并重试")

def main():
    """主函数"""
    quick_start = QuickStart()

    # 检查是否在虚拟环境中
    if sys.prefix == sys.base_prefix:
        print("⚠️  警告: 当前不在虚拟环境中")
        print("   建议先激活虚拟环境:")
        print("   source venv/bin/activate  # Linux/Mac")
        print("   venv\\Scripts\\activate     # Windows")
        print()
        response = input("是否继续？(y/n): ")
        if response.lower() != 'y':
            print("已取消")
            return

    quick_start.run()

if __name__ == "__main__":
    main()
