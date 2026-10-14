# 分支治理纪律

> 来源：整改开发计划（2026-10-13）§0 执行约定。起因是 f1518c1 revert 事故——对一次
> 已合入的 merge 做了无影响面评估的整段 `git revert <merge>`，把该 merge 连带引入的
> 无关内容（含 README/QUICK_START 重写）一并冲掉。以下纪律自该计划起对所有工作包生效。

## 1. 分支模型

- **基线**：远程以 gitlab 为准，主分支为 `gitlab/main`。
- **拉分支**：特性分支一律从 `gitlab/main` 拉取（先 `git fetch gitlab && git switch -c feat/<topic> gitlab/main`），不基于本地陈旧分支或其它特性分支起任务。
- **合入**：一律 MR 回 `main`，由 CI（check / build / deploy dev）与测试角色验收后合并，禁止本地直接 push main。

## 2. 禁止重复 merge 同一特性分支

**禁止**将同一特性分支多次 merge 回 main。分支合入后若需继续改动：

- 小改动：新开分支从 `gitlab/main` 拉取；
- 同主题延续：合入前 rebase 到最新 `gitlab/main`，或在原 MR（合并请求）上更新推送，让 MR 保持单次合入。

多次 merge 同一分支会让 revert 该 merge 的影响面无法静态确定，是本次整改要消除的核心事故模式。

## 3. revert merge 的影响面清单纪律

**禁止**无影响面评估的整段 `git revert <merge>`。revert 任何 merge 前必须：

1. 用 `git show <merge> --stat`（或 `git diff <merge>^1..<merge> --stat`）列出该 merge 引入的**全部文件清单**；
2. 逐项确认归属：哪些是本次确实要回退的，哪些是无辜的连带内容；
3. 无辜连带内容必须有恢复计划（恢复提交或重新 MR），并在 revert 提交信息中列明；
4. revert 后跑受影响模块的测试，确认连带内容按计划恢复。

## 4. 提交纪律

- **禁止 `git add -A`**（以及 `git add .`）。显式 `git add <文件>` 列出每个要提交的文件，提交前 `git status` 核对暂存区内容与提交意图一致。
- 提交信息用中文，格式 `feat(w{NN}): ...` / `test(w{NN}): ...`（`w{NN}` 为工作包号）。
- 仓库已有 `.gitignore` 覆盖运行产物（`out/`、`output/`、`uploads/`、`.zcode/` 等，见 .gitignore「本地运行/工具产物」小节）；新增易误提交的产物路径时先补 `.gitignore` 再提交。

## 5. 远程与镜像线

- 默认只推 gitlab 远程；GitHub 镜像同步仅在明确要求时按既有流程执行，不随日常开发推送。
- 双线历史相互独立：gitlab 侧做历史重写（如 D-2 运维窗口的 `git filter-repo` 大 blob 清理）时，GitHub 镜像线按既有同步流程单独重做，互不影响。

## 6. 历史重写（filter-repo 运维窗口）附加纪律

git filter-repo 需 force-push，属 D-2 单列的运维任务，须避开迭代高峰、全员停推窗口内执行：

1. 窗口内全员停止 push，通知到位后才开始；
2. 重写完成 force-push gitlab 后，发布全员重克隆声明；
3. 所有本地陈旧分支（尤其本地 main）必须强制对齐到重写后的远程（`git fetch && git reset --hard gitlab/main`），禁止把旧树回推到任何远程；
4. GitHub 镜像线单独重做 squash 同步。

## 7. 开发隔离

- 大改动（当前约定：W3 行级落库、W15 容器隔离、W17 评测域重构）必须用 `git worktree` 隔离开发（`.worktrees/` 已在 .gitignore），主工作区保持干净；
- 其余工作包用普通特性分支即可。
