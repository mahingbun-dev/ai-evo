# 插件评测 · 按分支评测（插件市场来源）

> 状态：已实现（2026-10-23）。入口：新建/编辑任务 → 评测类型「插件评测」→
> 评测对象来源「插件市场」→ 评测分支。

## 一、解决什么

市场仓库里同一插件往往有多个分支（main / feature / release）。此前市场只克隆一份
本地副本（建市场时登记的 `branch`，默认 main），所有评测都评这一份。现在可以在任务上
指定分支，评测使用**该分支自己的独立副本**，并支持多个用户同时评测不同分支。

## 二、行为口径

- `plugin_config.market_branch`：分支名；**留空 = 市场默认分支**（市场登记的
  `branch`，没登记则远端 HEAD）。存量任务不带该字段，行为与改动前完全一致。
- 首次选择某分支：后台 `git clone --depth 1 --branch <分支>` 拉到独立目录（最新提交）。
- 该分支已有本地副本：**直接复用，不再拉取**。副本建好后不再改写（评测只读），
  因此同一分支的并发评测共享同一份目录是安全的。
- 评测对象的插件列表（`GET /plugin-marketplaces/{id}/plugins?branch=`）也从
  该分支副本读取：分支上新增/删除的插件按分支真实呈现。
- 行结果的 `market_info` 记录 `branch` / `branch_selected` / `repo_root` / `commit`，
  回答「这个分是哪个分支哪个提交」。

## 三、并发设计（关键）

```
uploads/plugin_marketplaces/
├── 1/                                  # 市场默认分支副本（原有，plugin-eval 评测动作从这里加载）
└── branches/
    └── 1/
        ├── main/                       # 分支副本（浅克隆）
        ├── feature%2Fplugin-x/         # 分支名里的 / 被 URL 编码成 %2F，单层目录
        └── .feature%2Fplugin-x.lock    # 分支级 flock 锁文件
```

- **不同分支 = 不同目录**：各用一份独立浅克隆，互不共享工作树，可并行评测。
- **同一分支首次并发**：`flock` 跨进程互斥，只有第一个进程克隆，其余进程等待后复用；
  克隆先落到 `.tmp-*` 临时目录再 `os.replace` 原子换名，不会出现半成品目录。
- **副本只读**：创建后不再 fetch / reset，运行中的评测不会遇到文件被改写。
- 评测动作 plugin-eval 本身仍从市场默认副本加载（不带分支），分支只决定被测插件
  的代码，不让被测分支的引擎版本影响口径。

## 四、分支名与安全

`validate_branch_name`：只接受 `[A-Za-z0-9][A-Za-z0-9._/-]{0,199}`，拒绝 `..`、`//`、
结尾 `.`、`.lock` 后缀。创建/更新任务时校验一次（`plugin_config.market_branch`），
API 与执行期再校验一次，避免目录穿越与非法 git 引用。

## 五、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/evaluation/plugin-marketplaces/{id}/branches` | 远端全部分支（默认分支置顶，含 commit 与是否已缓存） |
| GET | `/evaluation/plugin-marketplaces/{id}/plugins?branch=<name>` | 按分支列出可评测插件/skill；首次会拉取该分支 |

删除市场时连同 `branches/{id}/` 一并清理。

## 六、限制

- 分支副本是**不可变缓存**：已有副本不会自动更新。要拉取分支的最新提交，删除
  `uploads/plugin_marketplaces/branches/{market_id}/{编码分支名}` 后重新评测即可
  （不要在有评测运行中删除，运行中的评测按绝对路径读取该目录）。
- 分支只在「插件市场」来源下由界面暴露；trace 口径沿用市场默认分支定位被测插件
  （`market_branch` 字段若显式给出也会生效）。
