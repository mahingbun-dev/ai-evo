/**
 * tests/unit 目录入口（node:test 聚合器）
 *
 * 为什么需要这个文件：
 * Node v23+ 的测试运行器把 `node --test` 的位置参数当作模块入口执行，
 * 不再把目录参数展开为目录内的测试文件（实测 Node v24.18.0：
 * `node --test tests/unit/` 直接以 CJS 目录入口方式加载 `tests/unit/`）。
 * Node 的模块目录解析规则会寻找 `index.js`，因此本文件让
 * `node --test tests/unit/` 这一门控命令继续可用，并在此聚合全部 node:test 测试。
 *
 * 新增测试文件后请在此追加一行 import（仅限 node:test 风格；
 * vitest 风格文件请放 tests/vitest-legacy/，见其 README）。
 */
import './claudeCodeMcpSwitch.test.mjs';
import './judge-pool-config.test.mjs';
import './modelsPreScriptAdmin.test.mjs';
import './refMarks.test.mjs';
import './resultsPreScript.test.mjs';
import './resultsUx.test.mjs';
