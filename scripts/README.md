# OpenCompass 脚本说明

本目录包含用于快速设置和启动 OpenCompass 项目的脚本。

## 脚本列表

### 1. setup.sh - Bash 设置脚本

**功能**: 自动化设置 OpenCompass 项目

**使用方法**:
```bash
# 给脚本添加执行权限
chmod +x scripts/setup.sh

# 在项目根目录运行
./scripts/setup.sh
```

**执行步骤**:
1. 检查 Python 版本
2. 检查是否在项目根目录
3. 创建虚拟环境（如果不存在）
4. 激活虚拟环境
5. 升级 pip
6. 安装所有依赖
7. 检查配置文件
8. 检查数据库配置
9. 创建必要目录
10. 检查测试数据文件
11. 检查 Allure 安装
12. 创建环境变量文件
13. 测试数据库连接
14. 运行简单测试验证安装

### 2. quick_start.py - Python 快速开始脚本

**功能**: 交互式命令行界面，帮助用户快速配置和启动项目

**使用方法**:
```bash
# 在项目根目录运行
python scripts/quick_start.py

# 或者
python3 scripts/quick_start.py
```

**特点**:
- 交互式界面
- 详细的步骤说明
- 错误处理和提示
- 生成示例测试数据
- 验证安装结果

### 3. 其他实用脚本

#### 数据库管理脚本

```bash
# 创建数据库表
python -c "
from app.db.session import engine, Base
Base.metadata.create_all(bind=engine)
print('数据库表创建成功')
"

# 查看数据库状态
python -c "
from app.db.session import SessionLocal
from app.models.models import PytestTask

db = SessionLocal()
tasks = db.query(PytestTask).all()
print(f'数据库中有 {len(tasks)} 个任务')
db.close()
"
```

#### 测试执行脚本

```bash
# 运行所有测试
pytest

# 运行端到端测试
pytest -m e2e

# 运行质量评估测试
pytest -m quality

# 运行一致性测试
pytest -m consistency

# 运行成功率测试
pytest -m success_rate

# 运行指定测试
pytest projects/testcase_creator/tests/test_e2e.py::TestE2E::test_testpoint_quality

# 生成 Allure 报告
pytest --alluredir=allure-results
allure serve allure-results
```

#### API 测试脚本

```bash
# 测试服务是否正常
curl http://localhost:8000

# 发现测试
curl "http://localhost:8000/api/v1/discover-tests"

# 运行测试
curl -X POST "http://localhost:8000/api/v1/run-test" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "projects/testcase_creator/tests/test_e2e.py::TestE2E::test_testpoint_quality",
    "args": ["-v", "-s"]
  }'
```

## 使用流程

### 方式一：使用 Bash 脚本（推荐）

```bash
# 1. 进入项目根目录
cd /path/to/opencompass

# 2. 运行设置脚本
./scripts/setup.sh

# 3. 编辑配置文件
vim .env
vim projects/testcase_creator/config.yaml

# 4. 启动服务
python -m app.main

# 5. 运行测试
pytest -m e2e
```

### 方式二：使用 Python 脚本

```bash
# 1. 进入项目根目录
cd /path/to/opencompass

# 2. 运行快速开始脚本
python scripts/quick_start.py

# 3. 编辑配置文件
vim .env
vim projects/testcase_creator/config.yaml

# 4. 启动服务
python -m app.main

# 5. 运行测试
pytest -m e2e
```

### 方式三：手动设置

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# 2. 安装依赖
pip install -r requirements.txt
pip install -e pytest_core/
pip install -r agent/requirements.txt
pip install -r pytest_core/requirements.txt

# 3. 配置环境变量
export DATABASE_URL="postgresql://username:password@host:port/database"

# 4. 创建必要目录
mkdir -p projects/testcase_creator/cache
mkdir -p projects/testcase_creator/data/history_testcases
mkdir -p allure-results

# 5. 启动服务
python -m app.main

# 6. 运行测试
pytest -m e2e
```

## 脚本参数说明

### setup.sh

无参数，交互式运行。

### quick_start.py

无参数，交互式运行。

## 常见问题

### 问题 1: 脚本执行权限不足

**解决方案**:
```bash
chmod +x scripts/setup.sh
```

### 问题 2: 虚拟环境创建失败

**解决方案**:
```bash
# 检查 Python 版本
python --version

# 手动创建虚拟环境
python -m venv venv

# 如果失败，尝试使用 virtualenv
pip install virtualenv
virtualenv venv
```

### 问题 3: 依赖安装失败

**解决方案**:
```bash
# 升级 pip
pip install --upgrade pip

# 逐个安装依赖
pip install fastapi
pip install pytest
pip install sqlalchemy
# ... 其他依赖

# 或者使用国内镜像源
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 问题 4: 数据库连接失败

**解决方案**:
```bash
# 检查数据库是否运行
sudo systemctl status postgresql  # Linux
brew services list | grep postgres  # macOS

# 检查数据库配置
echo $DATABASE_URL

# 测试数据库连接
psql $DATABASE_URL
```

### 问题 5: Allure 未安装

**解决方案**:
```bash
# macOS
brew install allure

# Ubuntu/Debian
sudo apt-get install allure

# Windows
choco install allure

# 或者下载二进制文件
# https://github.com/allure-framework/allure2/releases
```

## 脚本调试

### 调试 setup.sh

```bash
# 查看脚本内容
cat scripts/setup.sh

# 逐行执行
bash -x scripts/setup.sh

# 只执行特定步骤
# 修改脚本，注释掉不需要的步骤
```

### 调试 quick_start.py

```bash
# 查看脚本内容
cat scripts/quick_start.py

# 添加调试信息
python -c "
import sys
sys.path.insert(0, '.')
from scripts.quick_start import QuickStart
qs = QuickStart()
qs.check_python_version()
"

# 运行并查看详细输出
python scripts/quick_start.py 2>&1 | tee setup.log
```

## 自定义脚本

### 创建自定义设置脚本

```bash
# 创建自定义脚本
cat > scripts/custom_setup.sh << 'EOF'
#!/bin/bash

# 自定义设置脚本
echo "自定义设置开始..."

# 你的自定义步骤
# ...

echo "自定义设置完成！"
EOF

chmod +x scripts/custom_setup.sh
```

### 创建自定义测试脚本

```bash
# 创建自定义测试脚本
cat > scripts/run_tests.sh << 'EOF'
#!/bin/bash

# 自定义测试脚本
echo "运行测试..."

# 运行特定测试
pytest projects/testcase_creator/tests/test_e2e.py::TestE2E::test_testpoint_quality -v

echo "测试完成！"
EOF

chmod +x scripts/run_tests.sh
```

## 脚本维护

### 更新脚本

```bash
# 拉取最新代码
git pull origin main

# 重新运行设置脚本
./scripts/setup.sh
```

### 贡献脚本改进

1. 修改脚本文件
2. 测试脚本功能
3. 提交到 Git 仓库
4. 创建 Pull Request

## 相关文档

- [完整使用说明](../USAGE_GUIDE.md)
- [快速开始指南](../QUICK_START.md)
- [项目 README](../README.md)

---

**脚本版本**: v1.0.0
**最后更新**: 2026-02-10
