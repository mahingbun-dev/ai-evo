/**
 * 脚本库页面（Scripts.vue，自 Models 页迁移）单元测试
 * 文件: frontend/tests/unit/modelsPreScriptAdmin.test.mjs
 * 运行: node --test frontend/tests/unit/modelsPreScriptAdmin.test.mjs
 *
 * 与验收一致的手法：提取 api/index.js 的 preJudgeScriptApi 对象字面量与
 * Scripts.vue「评测前处理脚本管理」区段的真实源码，在受控上下文中求值：
 * - D1 导入收口契约：导入仅剩 .zip 包 / 文件夹直选两通道，统一走 multipart
 *   importPackage（files 与 relative_paths 一一对应 + overwrite，传原始相对路径，
 *   服务端对原始路径再做同样唯一顶层目录剥离故幂等）；api 层不再存在
 *   importScripts（JSON 契约导入端点 POST /pre-judge-scripts/import 已删除）；
 *   前端预检（根级缺 main.py/model.py、文件数>200、总量>10MB、单文件>2MB）
 *   报错不出树；main.py/model.py 树节点 disabled 锁定必选；
 *   __pycache__/.pyc/.pyo/`.`开头隐藏路径等噪音文件过滤不进树
 * - D2 导入结果：import-package 响应行渲染（formatPackageResultLine）与
 *   提交结果展示；原 json 契约导入的摘要推导（formatImportSummary）已随
 *   通道下线一并删除
 * - D3 管理员门控：写操作按钮挂 isSuperAdmin（App.vue inject 同一数据源）
 * - D4 删除为物理删除：确认文案如实说明（被评测任务引用不可删）
 * - D5 统一脚本包形态：列表文件状态列（file_count）、脚手架三选项
 *   （createFromTemplate）、在线保存乐观锁（saveFile updated_at / 409）、
 *   导出以 zip 包为主
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const apiSrc = readFileSync(join(here, '../../src/api/index.js'), 'utf-8')
const vueSrc = readFileSync(join(here, '../../src/views/Scripts.vue'), 'utf-8')

// ---------------- 源码提取工具 ----------------

/** 从 from 下标起做引号感知的花括号配对，返回完整对象/块源码 */
function extractBalanced(src, from) {
  let depth = 0
  let quote = null
  for (let i = from; i < src.length; i++) {
    const ch = src[i]
    if (quote) {
      if (ch === '\\') { i++; continue }
      if (ch === quote) quote = null
      continue
    }
    if (ch === "'" || ch === '"' || ch === '`') { quote = ch; continue }
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) return src.slice(from, i + 1)
    }
  }
  throw new Error('花括号未闭合：源码提取失败')
}

/** 提取 api/index.js 的 preJudgeScriptApi 对象字面量并用受控 request 求值 */
function buildApi(requestStub) {
  const mark = 'export const preJudgeScriptApi = '
  const idx = apiSrc.indexOf(mark)
  if (idx < 0) throw new Error('未在 api/index.js 中定位到 preJudgeScriptApi')
  const literal = extractBalanced(apiSrc, idx + mark.length)
  return new Function('request', `return (${literal})`)(requestStub)
}

/** axios 实例形态的 request 桩：request.post(url, data, config) 等，记录全部调用 */
function recordingRequest(captured) {
  const make = (method) => (url, data, config) => {
    captured.push({ method, url, data, config })
    return Promise.resolve({ data: {} })
  }
  return { get: make('get'), post: make('post'), put: make('put'), delete: make('delete') }
}

/** 提取 Scripts.vue「评测前处理脚本管理」区段真实源码并在受控上下文求值 */
const START_MARK = '// ==================== 评测前处理脚本管理 ===================='
const END_MARK = '// ---------- 脚本试跑（debug） ----------'
const startIdx = vueSrc.indexOf(START_MARK)
const endIdx = vueSrc.indexOf(END_MARK)
if (startIdx < 0 || endIdx < 0 || endIdx <= startIdx) {
  throw new Error('未能在 Scripts.vue 中定位「评测前处理脚本管理」区段（源码结构可能已变化，请修正测试锚点）')
}

function buildView({
  confirmBehavior = 'resolve',
  zipFiles = null,       // JSZip.loadAsync 返回的 zip.files 桩：{ 路径: { dir?, bytes? } }
  zipLoadError = null,   // 非 null 时 loadAsync 直接抛错（模拟非法 zip）
  importPackageResults,  // importPackage 桩返回的 results（缺省为一行成功结果）
} = {}) {
  const calls = {
    message: [], confirm: [],
    api: { create: [], update: [], delete: [], importPackage: [],
           list: [], listFiles: [], readFile: [], saveFile: [], createFile: [],
           deleteFile: [], renameFile: [], createFromTemplate: [] }
  }
  const results = importPackageResults !== undefined ? importPackageResults
    : [{ source: 'pkg', status: 'ok', script_id: 'pkg-1', name: 'pkg', warnings: [] }]
  const ctx = {
    ref: (v) => ({ value: v }),
    computed: (fn) => ({ get value() { return fn() } }),
    watch: (_getter, _cb) => {},
    inject: (_key, defaultValue) => defaultValue,
    nextTick: async (fn) => { if (fn) fn() },
    ElMessage: {
      success: (m) => calls.message.push(['success', m]),
      warning: (m) => calls.message.push(['warning', m]),
      error: (m) => calls.message.push(['error', m]),
    },
    ElMessageBox: {
      confirm: (message) => {
        calls.confirm.push(message)
        return confirmBehavior === 'resolve' ? Promise.resolve() : Promise.reject(new Error('cancel'))
      },
      prompt: (message) => {
        calls.confirm.push(message)
        return Promise.resolve({ value: '' })
      },
    },
    // zip 本地解压桩（真实实现为 JSZip.loadAsync(file) → zip.files[path].async('uint8array')）
    JSZip: {
      loadAsync: async () => {
        if (zipLoadError) throw new Error(zipLoadError)
        const files = {}
        for (const [p, e] of Object.entries(zipFiles || {})) {
          files[p] = { dir: !!e.dir, async: async () => new Uint8Array(e.bytes || [120]) }
        }
        return { files }
      },
    },
    preJudgeScriptApi: {
      list: async () => (calls.api.list.push('list'), { total: 0, items: [] }),
      get: async () => ({}),
      create: async (data) => (calls.api.create.push(data), { id: 'new-1', updated_at: 'T0', ...data }),
      update: async (id, data) => (calls.api.update.push([id, data]), { id, updated_at: 'T1', ...data }),
      delete: async (id) => (calls.api.delete.push(id), { deleted: true }),
      debug: async () => ({}),
      export: async () => new Blob(['{}']),
      uploadFile: async (id, formData) => { calls.api.uploadFile = [[id, formData]] },
      createFromTemplate: async (data) => (calls.api.createFromTemplate.push(data),
        { id: 'tpl-1', updated_at: 'T0', exec_type: 'script', entrypoint: 'main.py', ...data }),
      importPackage: async (formData) => (calls.api.importPackage.push(formData), { results }),
      exportPackage: async (id) => (calls.api.deleteFile.push('exportPackage:' + id), new Blob(['zip'])),
      listFiles: async (id) => (calls.api.listFiles.push(id), { files: [{ path: 'main.py', size: 10 }] }),
      readFile: async (id, path) => (calls.api.readFile.push([id, path]), { path, content: '# main\n' }),
      saveFile: async (id, data) => (calls.api.saveFile.push([id, data]),
        calls.api.saveFileResult || { path: data.path, updated_at: 'T2', warnings: [] }),
      createFile: async (id, data) => (calls.api.createFile.push([id, data]), { path: data.path, created: true }),
      deleteFile: async (id, path) => (calls.api.deleteFile.push([id, path]), { path, deleted: true }),
      renameFile: async (id, data) => (calls.api.renameFile.push([id, data]),
        { path: data.path, new_path: data.new_path, updated_at: 'T3' }),
    },
  }
  const body = vueSrc.slice(startIdx, endIdx)
  const factory = new Function(
    ...Object.keys(ctx),
    `${body}\nreturn {
      preJudgeScripts, preScriptsLoading, isSuperAdmin,
      execTypeLabel, loadPreJudgeScripts,
      scriptDialogVisible, isScriptEdit, editingScript, scriptFormRef, scriptForm,
      scriptFiles, activeFilePath, activeFileContent, currentEditorText, editorDirty,
      baselineUpdatedAt, editorActiveTab, hasModelPy,
      scriptInputFieldRows, scriptOutputSchemaRows,
      openScriptDialog, loadScriptFiles, selectScriptFile, saveActiveFile,
      saveScriptMeta,
      togglePreScriptActive, deletePreScript,
      scriptCreateVisible, scriptCreateFormRef, scriptCreateForm, submitCreateScript,
      // 导入（.zip 包 / 文件夹直选 + 树预览 + 预检）
      scriptImportVisible, importOverwrite, importResultText, importLoading,
      importPreview, importPrecheckErrors, importTreeRef,
      IMPORT_LOCKED_FILES, IMPORT_MAX_TOTAL_SIZE, IMPORT_MAX_FILE_COUNT, IMPORT_MAX_FILE_SIZE,
      stripCommonTopDir, isImportNoisePath, buildImportPreview, buildImportTree, formatImportSize,
      openScriptImportDialog, clearImportPreview, submitScriptImport, callImportPackage,
      formatPackageResultLine, handleZipPick, handleFolderPick,
    };`
  )
  return { api: factory(...Object.values(ctx)), calls, ctx }
}

// 给 File 补 webkitRelativePath（浏览器文件夹直选才有该属性）
function folderFile(relativePath, content = 'x') {
  const file = new File([content], relativePath.split('/').pop())
  Object.defineProperty(file, 'webkitRelativePath', { value: relativePath })
  return file
}

/** 构造 buildImportPreview 的 entries 项（body 用 File 模拟，size 可控） */
function pkgEntry(relPath, size = 10) {
  return { relPath, size, body: new File(['x'.repeat(size)], relPath.split('/').pop()) }
}

/** 从树数据收集叶子文件节点（key 不以 '/' 结尾且非 __root__） */
function collectLeafFileNodes(treeData) {
  const out = []
  const walk = (nodes) => {
    for (const n of nodes || []) {
      if (n.children && n.children.length) walk(n.children)
      else if (n.key && n.key !== '__root__') out.push(n)
    }
  }
  walk(treeData)
  return out
}

// ---------------- D1 导入收口契约 ----------------

describe('D1 导入收口契约（仅 .zip 包 / 文件夹直选，统一 multipart import-package）', () => {
  it('api 层不再存在 importScripts：已删除端点 POST /pre-judge-scripts/import 无任何残留', () => {
    const captured = []
    const api = buildApi(recordingRequest(captured))
    assert.equal(api.importScripts, undefined, 'importScripts 死代码应已删除')
    assert.ok(!apiSrc.includes("'/pre-judge-scripts/import'"),
      'api 源码不得再指向已删除的 JSON 契约导入端点')
    assert.ok(!vueSrc.includes('preJudgeScriptApi.importScripts'), '视图不得再引用 importScripts')
    assert.ok(!vueSrc.includes('importScripts'), '视图源码不应残留 importScripts 标识符')
  })

  it('importPackage 为 multipart POST /pre-judge-scripts/import-package（服务端期望 files + relative_paths 表单字段）', () => {
    const captured = []
    const api = buildApi(recordingRequest(captured))
    const fd = new FormData()
    fd.append('files', new File(['x'], 'a.py'))
    fd.append('relative_paths', 'a.py')
    fd.append('overwrite', 'true')
    api.importPackage(fd)
    assert.equal(captured[0].method, 'post')
    assert.equal(captured[0].url, '/pre-judge-scripts/import-package')
    assert.equal(captured[0].data, fd)
    assert.equal(captured[0].config?.headers?.['Content-Type'], 'multipart/form-data')
  })

  it('Scripts.vue 提交导入：勾选叶子重建 multipart，files 与 relative_paths 一一对应且传原始相对路径', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.buildImportPreview('已选脚本包', 'pkg.zip', [
      pkgEntry('pkg/main.py'), pkgEntry('pkg/model.py'),
      pkgEntry('pkg/utils.py'), pkgEntry('pkg/README.md'),
    ])
    // 模拟用户在树中取消勾选 README.md（叶子级勾选；main.py/model.py disabled 无法取消）
    const leaves = collectLeafFileNodes(api.importPreview.value.treeData)
    api.importTreeRef.value = {
      getCheckedNodes: (leafOnly) => (leafOnly ? leaves.filter(n => n.key !== 'README.md') : leaves),
    }
    await api.submitScriptImport()
    assert.equal(calls.api.importPackage.length, 1)
    const fd = calls.api.importPackage[0]
    const rels = fd.getAll('relative_paths')
    assert.deepEqual([...rels].sort(),
      ['pkg/main.py', 'pkg/model.py', 'pkg/utils.py'],
      '传原始相对路径（服务端再做同样唯一顶层目录剥离，幂等）')
    const files = fd.getAll('files')
    assert.equal(files.length, rels.length, 'files 与 relative_paths 一一对应')
    assert.ok(files.every(f => f instanceof File), 'zip 条目重建为 File（携带文件名）')
    assert.deepEqual(files.map(f => f.name).sort(), ['main.py', 'model.py', 'utils.py'])
    assert.equal(fd.get('overwrite'), 'false')
  })

  it('锁定文件兜底：勾选集缺失 main.py/model.py 时提交仍必含（disabled 节点不可取消）', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.buildImportPreview('已选脚本包', 'pkg.zip', [
      pkgEntry('pkg/main.py'), pkgEntry('pkg/model.py'), pkgEntry('pkg/utils.py'),
    ])
    const leaves = collectLeafFileNodes(api.importPreview.value.treeData)
    // 极端模拟：勾选集只剩非锁定文件（disabled 兜底逻辑必须补回锁定文件）
    api.importTreeRef.value = {
      getCheckedNodes: () => leaves.filter(n => n.key === 'utils.py'),
    }
    await api.submitScriptImport()
    const fd = calls.api.importPackage[0]
    const rels = fd.getAll('relative_paths')
    assert.deepEqual([...rels].sort(), ['pkg/main.py', 'pkg/model.py', 'pkg/utils.py'])
  })

  it('锁定树节点：main.py/model.py 节点 disabled:true 且默认全选（checkedKeys 含全部路径）', () => {
    const { api } = buildView()
    api.buildImportPreview('已选脚本包', 'pkg.zip', [
      pkgEntry('pkg/main.py'), pkgEntry('pkg/model.py'), pkgEntry('pkg/utils.py'),
    ])
    const leaves = collectLeafFileNodes(api.importPreview.value.treeData)
    const byKey = Object.fromEntries(leaves.map(n => [n.key, n]))
    assert.equal(byKey['main.py'].disabled, true, 'main.py 锁定')
    assert.equal(byKey['model.py'].disabled, true, 'model.py 锁定')
    assert.equal(byKey['utils.py'].disabled, false, '普通文件不锁定')
    assert.deepEqual([...api.importPreview.value.checkedKeys].sort(),
      ['main.py', 'model.py', 'utils.py'], '默认全选')
  })

  it('预检：根级缺 model.py / 缺 main.py 报错且不出树、不发起请求', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.buildImportPreview('已选脚本包', 'pkg.zip', [pkgEntry('pkg/main.py'), pkgEntry('pkg/utils.py')])
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('缺少根级 model.py')),
      api.importPrecheckErrors.value.join('；'))
    assert.equal(api.importPreview.value, null, '预检报错不出树')
    await api.submitScriptImport()
    assert.equal(calls.api.importPackage.length, 0, '预检失败不发起请求')

    api.buildImportPreview('已选脚本包', 'pkg2.zip', [pkgEntry('pkg2/model.py')])
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('缺少根级 main.py')),
      api.importPrecheckErrors.value.join('；'))
    assert.equal(api.importPreview.value, null)
  })

  it('预检：文件数>200 / 总量>10MB / 单文件>2MB 各自报错且不出树', () => {
    const { api } = buildView()
    // 文件数超限：main.py + model.py + 199 个其他文件 = 201
    const many = [pkgEntry('p/main.py'), pkgEntry('p/model.py')]
    for (let i = 0; i < 199; i++) many.push(pkgEntry(`p/f${i}.py`))
    api.buildImportPreview('已选脚本包', 'many.zip', many)
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('文件数超限（201 > 200）')),
      api.importPrecheckErrors.value.join('；'))
    assert.equal(api.importPreview.value, null)

    // 总量超限：6 × 1.8MB = 10.8MB（单文件均不超 2MB，隔离出总量维度）
    const heavy = [pkgEntry('p/main.py', 10), pkgEntry('p/model.py', 10)]
    for (let i = 0; i < 6; i++) heavy.push(pkgEntry(`p/big${i}.py`, 1800 * 1024))
    api.buildImportPreview('已选脚本包', 'heavy.zip', heavy)
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('文件总大小超限')),
      api.importPrecheckErrors.value.join('；'))
    assert.equal(api.importPreview.value, null)

    // 单文件超限：一个 3MB 文件（总量未超，隔离出单文件维度）
    api.buildImportPreview('已选脚本包', 'big.zip', [
      pkgEntry('p/main.py'), pkgEntry('p/model.py'), pkgEntry('p/huge.py', 3 * 1024 * 1024),
    ])
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('单文件超限') && e.includes('p/huge.py')),
      api.importPrecheckErrors.value.join('；'))
    assert.equal(api.importPreview.value, null)
  })

  it('预检通过：正常包 errors 为空、树构建完整、可提交并按勾选重建 multipart', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.buildImportPreview('已选脚本包', 'ok.zip', [
      pkgEntry('ok/main.py'), pkgEntry('ok/model.py'), pkgEntry('ok/sub/util.py'),
    ])
    assert.deepEqual(api.importPrecheckErrors.value, [], '正常包无预检错误')
    assert.ok(api.importPreview.value, '预检通过出树')
    assert.equal(api.importPreview.value.fileCount, 3)
    const leaves = collectLeafFileNodes(api.importPreview.value.treeData)
    api.importTreeRef.value = { getCheckedNodes: () => leaves }
    await api.submitScriptImport()
    assert.equal(calls.api.importPackage.length, 1, '树可提交')
    const fd = calls.api.importPackage[0]
    assert.deepEqual([...fd.getAll('relative_paths')].sort(),
      ['ok/main.py', 'ok/model.py', 'ok/sub/util.py'])
  })

  it('文件夹直选 handleFolderPick：剥唯一顶层展示（rootLabel=顶层目录名），提交传原始相对路径', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.handleFolderPick({
      target: {
        files: [folderFile('myproj/main.py'), folderFile('myproj/model.py'), folderFile('myproj/sub/u.py')],
        value: '',
      },
    })
    const preview = api.importPreview.value
    assert.ok(preview, '文件夹直选出预览')
    assert.equal(preview.rootLabel, 'myproj', '剥离时树根显示顶层目录名')
    assert.equal(preview.fileCount, 3)
    assert.ok(preview.fileEntries.has('main.py'), '展示路径已剥离顶层目录')
    assert.equal(preview.fileEntries.get('main.py').originalPath, 'myproj/main.py', '提交用原始相对路径')
    const leaves = collectLeafFileNodes(preview.treeData)
    api.importTreeRef.value = { getCheckedNodes: () => leaves }
    await api.submitScriptImport()
    const fd = calls.api.importPackage[0]
    assert.deepEqual([...fd.getAll('relative_paths')].sort(),
      ['myproj/main.py', 'myproj/model.py', 'myproj/sub/u.py'])
    assert.ok(fd.getAll('files').every(f => f instanceof File), '文件夹直选直接用 File 作提交体')
  })

  it('zip 本地解压 handleZipPick：JSZip 条目构建预览（目录条目跳过），选择后重置 input', async () => {
    const { api } = buildView({
      zipFiles: { 'z/main.py': {}, 'z/model.py': {}, 'z/sub/': { dir: true }, 'z/README.md': {} },
    })
    const event = { target: { files: [new File(['PK'], 'z.zip')], value: 'stale' } }
    await api.handleZipPick(event)
    assert.equal(event.target.value, '', '选择后重置 input 允许重复选择同一文件')
    const preview = api.importPreview.value
    assert.ok(preview, 'zip 条目出预览')
    assert.equal(preview.fileCount, 3, '目录条目不参与导入')
    assert.equal(preview.rootLabel, 'z')
    assert.ok(preview.fileEntries.has('main.py') && preview.fileEntries.has('README.md'))
  })

  it('zip 解析失败：报「zip 解析失败」且不出树', async () => {
    const { api } = buildView({ zipLoadError: '文件不是合法的 zip 包' })
    await api.handleZipPick({ target: { files: [new File(['bad'], 'bad.zip')], value: '' } })
    assert.equal(api.importPreview.value, null)
    assert.ok(api.importPrecheckErrors.value.some(e => e.includes('zip 解析失败')),
      api.importPrecheckErrors.value.join('；'))
  })

  it('stripCommonTopDir 剥离口径：唯一顶层且全带 / 才剥离；根级散文件不剥离', () => {
    const { api } = buildView()
    // 唯一顶层 + 所有路径带 '/' → 剥离
    assert.deepEqual(
      api.stripCommonTopDir(['pkg/main.py', 'pkg/model.py', 'pkg/sub/u.py']),
      { root: 'pkg', paths: ['main.py', 'model.py', 'sub/u.py'] })
    // 根级散文件（裸文件名）→ 不剥离
    assert.deepEqual(
      api.stripCommonTopDir(['main.py', 'model.py']),
      { root: '', paths: ['main.py', 'model.py'] })
    // 共享顶层但存在裸顶层名（根级散文件形态）→ 不剥离
    assert.deepEqual(
      api.stripCommonTopDir(['pkg/main.py', 'pkg/model.py', 'pkg']),
      { root: '', paths: ['pkg/main.py', 'pkg/model.py', 'pkg'] })
    // 空列表 / 多顶层 → 不剥离
    assert.deepEqual(api.stripCommonTopDir([]), { root: '', paths: [] })
    assert.deepEqual(
      api.stripCommonTopDir(['a/main.py', 'b/model.py']),
      { root: '', paths: ['a/main.py', 'b/model.py'] })
  })

  it('噪音过滤：__pycache__ / .pyc / .pyo / . 开头隐藏路径不进树，计入已忽略数', () => {
    const { api } = buildView()
    assert.equal(api.isImportNoisePath('__pycache__/x.pyc'), true)
    assert.equal(api.isImportNoisePath('sub/__pycache__/y.py'), true)
    assert.equal(api.isImportNoisePath('.DS_Store'), true)
    assert.equal(api.isImportNoisePath('.git/config'), true)
    assert.equal(api.isImportNoisePath('sub/.hidden.txt'), true)
    assert.equal(api.isImportNoisePath('a.pyc'), true)
    assert.equal(api.isImportNoisePath('b.PYO'), true, '编译产物后缀不区分大小写')
    assert.equal(api.isImportNoisePath('main.py'), false)
    assert.equal(api.isImportNoisePath('sub/keep.py'), false)
    assert.equal(api.isImportNoisePath('.pycache'), true, '. 开头的段为隐藏路径')

    // 集成：噪音文件被过滤、不进树、计入 filteredCount
    api.buildImportPreview('已选脚本包', 'n.zip', [
      pkgEntry('n/main.py'), pkgEntry('n/model.py'),
      pkgEntry('n/__pycache__/m.pyc'), pkgEntry('n/.DS_Store'), pkgEntry('n/.git/config'),
    ])
    assert.ok(api.importPreview.value, '噪音过滤后剩余文件可正常出树')
    assert.equal(api.importPreview.value.fileCount, 2)
    assert.equal(api.importPreview.value.filteredCount, 3, '已忽略 3 个噪音文件')
    const keys = collectLeafFileNodes(api.importPreview.value.treeData).map(n => n.key)
    assert.ok(!keys.some(k => k.includes('__pycache__') || k.startsWith('.')), '噪音文件不进树')
  })
})

// ---------------- D2 导入结果渲染 ----------------

describe('D2 导入结果渲染（import-package 响应）', () => {
  it('结果行读取 imported_as 权威标志：created/updated/revived 各自语义', () => {
    const { api } = buildView()
    assert.ok(api.formatPackageResultLine({ status: 'ok', source: 'pkg', name: 'n1', imported_as: 'created' }).includes('新建成功'))
    assert.ok(api.formatPackageResultLine({ status: 'ok', source: 'pkg', name: 'n1', imported_as: 'updated' }).includes('覆盖更新成功'))
    assert.ok(api.formatPackageResultLine({ status: 'ok', source: 'pkg', name: 'n1', imported_as: 'revived' }).includes('同名停用脚本复活启用'))
    // 旧响应无 imported_as 时回退「导入成功」
    assert.ok(api.formatPackageResultLine({ status: 'ok', source: 'pkg', name: 'n1' }).includes('导入成功'))
    assert.ok(api.formatPackageResultLine({ status: 'error', source: 'pkg', error: '缺 main.py' }).includes('导入失败（缺 main.py）'))
  })
  it('结果行携带 warnings 提示与脚本名', () => {
    const { api } = buildView()
    const line = api.formatPackageResultLine({
      status: 'ok', source: 'pkg', name: 'n1', imported_as: 'created', warnings: ['缺 README', '依赖未声明'],
    })
    assert.ok(line.includes('脚本=n1'), line)
    assert.ok(line.includes('提示：缺 README；依赖未声明'), line)
  })
  it('提交后结果行写入 importResultText，成功提示并刷新列表', async () => {
    const { api, calls } = buildView()
    await api.openScriptImportDialog()
    api.buildImportPreview('已选脚本包', 'ok.zip', [pkgEntry('ok/main.py'), pkgEntry('ok/model.py')])
    const leaves = collectLeafFileNodes(api.importPreview.value.treeData)
    api.importTreeRef.value = { getCheckedNodes: () => leaves }
    await api.submitScriptImport()
    assert.equal(api.importResultText.value, 'pkg：导入成功（脚本=pkg）', api.importResultText.value)
    assert.ok(calls.message.some(([kind, m]) => kind === 'success' && m === '导入完成，详见导入结果'))
    assert.ok(calls.api.list.length >= 1, '导入成功后刷新脚本列表')
  })
  it('服务端未返回结果 → 失败文案兜底；全部失败 → error 提示', async () => {
    const empty = buildView({ importPackageResults: [] })
    await empty.api.openScriptImportDialog()
    empty.api.buildImportPreview('已选脚本包', 'e.zip', [pkgEntry('e/main.py'), pkgEntry('e/model.py')])
    const leaves = collectLeafFileNodes(empty.api.importPreview.value.treeData)
    empty.api.importTreeRef.value = { getCheckedNodes: () => leaves }
    await empty.api.submitScriptImport()
    assert.ok(empty.api.importResultText.value.includes('导入失败（服务端未返回结果）'),
      empty.api.importResultText.value)
    assert.ok(empty.calls.message.some(([kind]) => kind === 'error'))

    const failed = buildView({
      importPackageResults: [{ status: 'error', source: 'pkg', error: '缺 model.py' }],
    })
    await failed.api.openScriptImportDialog()
    failed.api.buildImportPreview('已选脚本包', 'f.zip', [pkgEntry('f/main.py'), pkgEntry('f/model.py')])
    const leaves2 = collectLeafFileNodes(failed.api.importPreview.value.treeData)
    failed.api.importTreeRef.value = { getCheckedNodes: () => leaves2 }
    await failed.api.submitScriptImport()
    assert.ok(failed.api.importResultText.value.includes('导入失败（缺 model.py）'),
      failed.api.importResultText.value)
    assert.ok(failed.calls.message.some(([kind, m]) => kind === 'error' && m === '导入失败，详见导入结果'))
  })
})

// ---------------- D3 管理员门控 ----------------

describe('D3 管理员门控（设计决策 #4）', () => {
  it('头部「导入」「新建脚本」按钮挂 v-if="isSuperAdmin"，行内写操作挂 !isSuperAdmin 禁用', () => {
    assert.equal(vueSrc.split('v-if="isSuperAdmin"').length - 1, 2, '导入与新建脚本两个按钮')
    assert.equal(vueSrc.split(':disabled="!isSuperAdmin').length - 1, 3, '编辑/停用启用/删除三个操作（!isSuperAdmin 门控在前，允许合并 || rowOpLoading）')
  })
  it('门控数据源与 App.vue 一致（inject isSuperAdmin），缺省按非管理员处理', () => {
    assert.ok(vueSrc.includes("const isSuperAdmin = inject('isSuperAdmin', computed(() => false))"))
  })
  it('只读操作（试跑/导出）不挂门控', () => {
    const debugBtn = /<el-button link type="primary"( :disabled="rowOpLoading")? @click="openScriptDebugDialog\(row\)">试跑<\/el-button>/.test(vueSrc)
    const exportBtn = /@click="exportPreScript\(row\)">导出<\/el-button>/.test(vueSrc)
    assert.ok(debugBtn && exportBtn)
    const rowOps = vueSrc.slice(vueSrc.indexOf('label="操作" min-width="300"'), vueSrc.indexOf('</el-table>\n    </el-card>'))
    assert.ok(!rowOps.includes('!isSuperAdmin" @click="openScriptDebugDialog'), '试跑不受门控')
    assert.ok(!rowOps.includes('!isSuperAdmin" @click="exportPreScript'), '导出不受门控')
  })
})

// ---------------- D4 删除为物理删除 ----------------

describe('D4 删除物理删除语义', () => {
  it('确认文案明确说明物理删除（记录与文件清除不可恢复、被引用需先解除关联）', async () => {
    const { api, calls } = buildView({ confirmBehavior: 'resolve' })
    await api.deletePreScript({ id: 'd1', name: '我的脚本' })
    assert.equal(calls.api.delete[0], 'd1')
    const msg = calls.confirm[0]
    assert.ok(msg.includes('物理删除'), msg)
    assert.ok(msg.includes('不可恢复'), msg)
    assert.ok(msg.includes('解除关联'), msg)
    assert.ok(calls.message.some(([, m]) => m.includes('脚本已删除')), '成功提示如实说明删除')
  })
  it('取消删除时不调用删除接口', async () => {
    const { api, calls } = buildView({ confirmBehavior: 'reject' })
    await api.deletePreScript({ id: 'd2', name: 'x' })
    assert.equal(calls.api.delete.length, 0)
  })
})

// ---------------- D5 统一脚本包形态：文件状态 / 脚手架 / 在线保存 / zip 导出 ----------------

describe('D5 统一脚本包形态', () => {
  it('列表「文件状态」列：script 类型按 file_count 展示「N 个文件」，0/缺失标「缺文件」', () => {
    assert.ok(vueSrc.includes("row.exec_type === 'script'"))
    assert.ok(vueSrc.includes('Number(row.file_count) > 0'))
    assert.ok(vueSrc.includes('>缺文件</el-tag>'))
  })

  it('脚手架：模板方式经 createFromTemplate 提交 {template, name, description}，空白走 create', async () => {
    const { api, calls } = buildView()
    api.scriptCreateFormRef.value = { validate: async () => true, clearValidate() {} }
    api.scriptCreateForm.value = { template: 'folder', name: 'my_scaffold', description: '描述' }
    await api.submitCreateScript()
    assert.equal(calls.api.createFromTemplate.length, 1)
    assert.deepEqual(calls.api.createFromTemplate[0], { template: 'folder', name: 'my_scaffold', description: '描述' })
    assert.equal(calls.api.create.length, 0, '模板方式不走空白 create')

    api.scriptCreateForm.value = { template: 'blank', name: 'my_blank', description: null }
    await api.submitCreateScript()
    assert.equal(calls.api.create.length, 1)
    assert.equal(calls.api.create[0].exec_type, 'script')
  })

  it('在线保存：saveFile 携带 {path, content, updated_at} 乐观锁；成功后更新基线并清除脏标记', async () => {
    const { api, calls } = buildView()
    api.editingScript.value = { id: 'e1', name: 's', exec_type: 'script', entrypoint: 'main.py', input_fields: {} }
    api.activeFilePath.value = 'main.py'
    api.activeFileContent.value = '# old\n'
    api.currentEditorText.value = '# new\n'
    api.editorDirty.value = true
    api.baselineUpdatedAt.value = 'T1'
    await api.saveActiveFile()
    const [id, payload] = calls.api.saveFile[0]
    assert.equal(id, 'e1')
    assert.deepEqual(payload, { path: 'main.py', content: '# new\n', updated_at: 'T1' })
    assert.equal(api.editorDirty.value, false, '保存成功清除脏标记')
    assert.equal(api.baselineUpdatedAt.value, 'T2', '乐观锁基线更新为响应值')
  })

  it('导出以 zip 包为主：script 类型行走 exportPackage，agent 类型回落契约 JSON', () => {
    assert.ok(vueSrc.includes('preJudgeScriptApi.exportPackage(row.id)'), '列表导出主入口为 zip 包')
    assert.ok(vueSrc.includes("row.exec_type === 'script'"), '按类型分流')
    assert.ok(vueSrc.includes('exportScriptPackageFromEditor'), '编辑窗口也有 zip 导出入口')
  })
})

// ---------------- 回归 ----------------

describe('回归：既有能力不受影响', () => {
  it('启停确认携带全量 payload（PUT 全量语义）', async () => {
    const { api, calls } = buildView()
    await api.togglePreScriptActive({ id: 't1', name: 's', exec_type: 'script', entrypoint: 'main.py', timeout_seconds: 60, is_active: true, description: null })
    const [id, payload] = calls.api.update[0]
    assert.equal(id, 't1')
    assert.equal(payload.is_active, false)
    assert.equal(payload.name, 's')
    assert.equal(payload.entrypoint, 'main.py')
  })
  it('基本信息保存（script 类型）不提交 entrypoint 覆盖（由服务端管理）', async () => {
    const { api, calls } = buildView()
    api.scriptFormRef.value = { validate: async () => true, clearValidate() {} }
    api.isScriptEdit.value = true
    api.editingScript.value = { id: 'm1', name: 'meta', exec_type: 'script', entrypoint: 'main.py', input_fields: {}, updated_at: 'T0' }
    api.scriptForm.value = { name: 'meta', description: '', exec_type: 'script', entrypoint: '', allowed_tools: [], timeout_seconds: 120, is_active: true }
    await api.saveScriptMeta()
    const [id, payload] = calls.api.update[0]
    assert.equal(id, 'm1')
    assert.equal(payload.entrypoint, 'main.py', '编辑保留服务端管理的 entrypoint')
    assert.equal(payload.exec_type, 'script')
  })
})

// ---------------- D6 编辑弹窗 Tab 化 + model.py 声明联动 ----------------

describe('D6 编辑弹窗 Tab 化与 model.py 声明联动', () => {
  it('源码结构：el-tabs 两 pane（basic 默认 / files），collapse 与 agent 占位已清除', () => {
    assert.ok(vueSrc.includes('<el-tabs v-model="editorActiveTab" class="editor-tabs">'))
    assert.ok(vueSrc.includes('label="基本信息" name="basic"'))
    assert.ok(vueSrc.includes('label="文件" name="files"'))
    assert.equal((vueSrc.match(/<el-tab-pane /g) || []).length, 2)
    assert.ok(!vueSrc.includes('metaCollapse'), '折叠面板状态应清除干净')
    assert.ok(!vueSrc.includes('meta-collapse'), '折叠面板样式应清除干净')
    assert.ok(!vueSrc.includes('editor-agent-placeholder'), 'agent 占位应清除干净')
    assert.ok(vueSrc.includes("const editorActiveTab = ref('basic')"), '默认打开基本信息')
  })
  it('源码结构：model.py 提示条挂 hasModelPy，切回文件 tab 矫正 CodeMirror 测量', () => {
    assert.ok(vueSrc.includes('v-if="hasModelPy"'))
    assert.ok(vueSrc.includes("if (tab === 'files') nextTick(() => editorView?.requestMeasure?.())"))
  })
  it('editorActiveTab 默认 basic，openScriptDialog 重置回 basic', () => {
    const { api } = buildView()
    assert.equal(api.editorActiveTab.value, 'basic')
    api.editorActiveTab.value = 'files'
    api.openScriptDialog({ id: 'r1', name: 'n', exec_type: 'script', entrypoint: 'main.py', updated_at: 'T0' })
    assert.equal(api.editorActiveTab.value, 'basic')
  })
  it('hasModelPy 随文件树推导（含 model.py 才提示）', () => {
    const { api } = buildView()
    assert.equal(api.hasModelPy.value, false)
    api.scriptFiles.value = [{ path: 'main.py' }, { path: 'model.py' }]
    assert.equal(api.hasModelPy.value, true)
  })
  it('保存 model.py 且响应带 schema_sync → 刷新声明并提示同步结果', async () => {
    const { api, calls } = buildView()
    api.editingScript.value = { id: 'mp1', name: 'mp', exec_type: 'script', entrypoint: 'main.py', updated_at: 'T0' }
    api.activeFilePath.value = 'model.py'
    api.currentEditorText.value = 'class PreScriptInput(BaseModel): ...'
    calls.api.saveFileResult = { path: 'model.py', updated_at: 'T2', warnings: [],
      schema_sync: { input_fields: { src: { value_type: 'local', description: '数据源' } },
                     output_schema: { hits: 'int：命中数' } } }
    await api.saveActiveFile()
    assert.deepEqual(calls.api.saveFile[0][1].path, 'model.py')
    assert.deepEqual(api.scriptInputFieldRows.value,
      [{ name: 'src', value_type: 'local', description: '数据源' }])
    // 输出说明已表单化（358fccc）：schema_sync 回写为行式树节点，
    // 'int：命中数' 叶串按 OUTPUT_TYPE_ALIASES 解析为 type=integer
    assert.deepEqual(api.scriptOutputSchemaRows.value,
      [{ name: 'hits', type: 'integer', description: '命中数', collapsed: false, children: [] }])
    assert.equal(api.editingScript.value.input_fields.src.value_type, 'local')
    assert.ok(calls.message.some(([, m]) => String(m).includes('model.py 声明已同步')))
  })
  it('保存非 model.py 文件：不触碰声明区（无 schema_sync 联动）', async () => {
    const { api, calls } = buildView()
    api.editingScript.value = { id: 'mp2', name: 'mp2', exec_type: 'script', entrypoint: 'main.py', updated_at: 'T0' }
    api.activeFilePath.value = 'utils.py'
    api.currentEditorText.value = 'x = 1'
    await api.saveActiveFile()
    assert.equal(calls.api.saveFile[0][1].path, 'utils.py')
    assert.deepEqual(api.scriptInputFieldRows.value, [])
    assert.ok(!calls.message.some(([, m]) => String(m).includes('声明已同步')))
  })
})
