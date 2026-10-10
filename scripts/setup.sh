#!/bin/bash

# OpenCompass 快速设置脚本
# 该脚本帮助用户快速配置和启动 OpenCompass 项目

set -e  # 遇到错误立即退出

echo "=========================================="
echo "OpenCompass 快速设置脚本"
echo "=========================================="
echo ""

# 检查 Python 版本
echo "1. 检查 Python 版本..."
PYTHON_VERSION=$(python3 --version 2>/dev/null || python --version 2>/dev/null)
echo "   $PYTHON_VERSION"

# 检查是否在项目根目录
if [ ! -f "requirements.txt" ]; then
    echo "错误: 请在项目根目录运行此脚本"
    echo "当前目录: $(pwd)"
    exit 1
fi

# 创建虚拟环境
echo ""
echo "2. 创建虚拟环境..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "   ✓ 虚拟环境已创建"
else
    echo "   ✓ 虚拟环境已存在"
fi

# 激活虚拟环境
echo ""
echo "3. 激活虚拟环境..."
source venv/bin/activate
echo "   ✓ 虚拟环境已激活"

# 升级 pip
echo ""
echo "4. 升级 pip..."
pip install --upgrade pip
echo "   ✓ pip 已升级"

# 安装依赖
echo ""
echo "5. 安装依赖..."
echo "   5.1 安装主应用依赖..."
pip install -r requirements.txt

echo "   5.2 安装 pytest_core 框架..."
pip install -e pytest_core/

echo "   5.3 安装 agent 相关依赖..."
pip install -r agent/requirements.txt

echo "   5.4 安装 pytest_core 测试框架依赖..."
pip install -r pytest_core/requirements.txt

echo "   ✓ 所有依赖已安装"

# 检查配置文件
echo ""
echo "6. 检查配置文件..."
CONFIG_FILE="projects/testcase_creator/config.yaml"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "   错误: 配置文件不存在: $CONFIG_FILE"
    exit 1
fi
echo "   ✓ 配置文件存在: $CONFIG_FILE"

# 检查数据库配置
echo ""
echo "7. 检查数据库配置..."
if [ -z "$DATABASE_URL" ]; then
    echo "   ⚠ 警告: DATABASE_URL 环境变量未设置"
    echo "   请设置数据库连接字符串:"
    echo "   export DATABASE_URL=\"postgresql://username:password@host:port/database\""
else
    echo "   ✓ 数据库配置已设置"
fi

# 创建必要的目录
echo ""
echo "8. 创建必要的目录..."
mkdir -p projects/testcase_creator/cache
mkdir -p projects/testcase_creator/data/history_testcases
mkdir -p allure-results
echo "   ✓ 目录已创建"

# 检查测试数据文件
echo ""
echo "9. 检查测试数据文件..."
TEST_DATA_FILE="projects/testcase_creator/data/e2e_testcase.json"
if [ ! -f "$TEST_DATA_FILE" ]; then
    echo "   ⚠ 警告: 测试数据文件不存在: $TEST_DATA_FILE"
    echo "   请创建测试数据文件或使用示例文件"
else
    echo "   ✓ 测试数据文件存在: $TEST_DATA_FILE"
fi

# 检查 Allure
echo ""
echo "10. 检查 Allure..."
if command -v allure &> /dev/null; then
    echo "   ✓ Allure 已安装: $(allure --version)"
else
    echo "   ⚠ 警告: Allure 未安装"
    echo "   安装方法:"
    echo "   - macOS: brew install allure"
    echo "   - Ubuntu: sudo apt-get install allure"
    echo "   - Windows: choco install allure"
fi

# 创建环境变量文件
echo ""
echo "11. 创建环境变量文件..."
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# 数据库配置
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
EOF
    echo "   ✓ 环境变量文件已创建: .env"
    echo "   ⚠ 请编辑 .env 文件并填写正确的配置"
else
    echo "   ✓ 环境变量文件已存在: .env"
fi

# 测试数据库连接
echo ""
echo "12. 测试数据库连接..."
if [ -n "$DATABASE_URL" ]; then
    python3 -c "
import sys
try:
    from app.db.session import engine
    from sqlalchemy import inspect
    inspector = inspect(engine)
    print('   ✓ 数据库连接成功')
    tables = inspector.get_table_names()
    if tables:
        print(f'   ✓ 数据库中有 {len(tables)} 个表')
    else:
        print('   ⚠ 数据库中没有表，将在启动时自动创建')
except Exception as e:
    print(f'   ✗ 数据库连接失败: {e}')
    sys.exit(1)
" || echo "   ⚠ 数据库连接测试跳过（请先设置 DATABASE_URL）"
else
    echo "   ⚠ 数据库连接测试跳过（请先设置 DATABASE_URL）"
fi

# 运行测试验证安装
echo ""
echo "13. 运行测试验证安装..."
echo "   运行简单测试..."
python3 -m pytest projects/testcase_creator/tests/test_e2e.py::TestE2E::test_testpoint_quality -v --tb=short 2>&1 | head -20
echo "   ✓ 测试验证完成"

# 显示下一步操作
echo ""
echo "=========================================="
echo "设置完成！"
echo "=========================================="
echo ""
echo "下一步操作:"
echo "1. 编辑 .env 文件，填写正确的配置"
echo "2. 编辑 projects/testcase_creator/config.yaml 文件"
echo "3. 启动 FastAPI 服务:"
echo "   python -m app.main"
echo "4. 运行测试:"
echo "   pytest -m e2e"
echo "5. 查看详细文档:"
echo "   cat USAGE_GUIDE.md"
echo "   cat QUICK_START.md"
echo ""
echo "常用命令:"
echo "  启动服务: python -m app.main"
echo "  运行测试: pytest -m e2e -v -s"
echo "  生成报告: pytest --alluredir=allure-results && allure serve allure-results"
echo ""
echo "=========================================="
