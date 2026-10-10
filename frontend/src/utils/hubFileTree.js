/**
 * anc-hub 执行包文件树组装工具（阶段D/F3/F4 共用）
 *
 * tree 接口返回扁平 file_index：[{path, type, size, is_nested_zip}]（posix 相对路径），
 * 前端组装为 el-tree 树形结构（目录节点 + 文件节点，一次构建）。
 */

// 文件类型 → 展示标签（与后端 _classify_file 分类对齐：text|image|archive|binary）
export const HUB_FILE_TYPE_LABELS = {
  text: '文本',
  image: '图片',
  archive: '压缩包',
  binary: '二进制'
}

// 文件大小格式化（B/KB/MB/GB）
export function formatHubFileSize(bytes) {
  if (bytes === null || bytes === undefined || bytes === '') return '-'
  const n = Number(bytes)
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(n) / Math.log(k))
  return parseFloat((n / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

/**
 * 把扁平 file_index 组装为 el-tree data
 * 目录节点：{key, label, isDir: true, children: [...]}
 * 文件节点：{key, label, isDir: false, path, type, size, isNested, isLeaf: true}
 * 目录下无重复名冲突（同一路径唯一），key 用完整 path 保证唯一
 */
export function buildHubTreeData(fileIndex) {
  const root = { __dirs: new Map(), __files: [] }
  const items = Array.isArray(fileIndex) ? fileIndex.filter(it => it && it.path) : []
  for (const item of items) {
    const parts = String(item.path).split('/').filter(Boolean)
    if (!parts.length) continue
    let node = root
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i]
      if (!node.__dirs.has(seg)) node.__dirs.set(seg, { __dirs: new Map(), __files: [] })
      node = node.__dirs.get(seg)
    }
    node.__files.push({ ...item, path: String(item.path) })
  }

  const toTreeNodes = (dirNode, prefix) => {
    const nodes = []
    for (const [seg, child] of dirNode.__dirs) {
      const path = prefix ? `${prefix}/${seg}` : seg
      const children = toTreeNodes(child, path)
      nodes.push({
        key: `dir:${path}`,
        label: seg,
        path,
        isDir: true,
        children,
        fileCount: countFiles(children)
      })
    }
    for (const f of dirNode.__files) {
      nodes.push({
        key: `file:${f.path}`,
        label: f.path.split('/').pop(),
        path: f.path,
        isDir: false,
        type: f.type || 'binary',
        size: f.size,
        isNested: !!f.is_nested_zip,
        // 人工补传标记（后端叠加视图打 is_manual；原始包文件不带），
        // uploaded_by/uploaded_at 一并透传供徽标 tooltip 展示
        isManual: !!f.is_manual,
        uploadedBy: f.uploaded_by || '',
        uploadedAt: f.uploaded_at || '',
        isLeaf: true
      })
    }
    // 目录在前、文件在后，均按名称排序
    nodes.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1
      return String(a.label).localeCompare(String(b.label), 'zh-Hans-CN')
    })
    return nodes
  }

  const countFiles = (children) => children.reduce((acc, n) => acc + (n.isDir ? countFiles(n.children || []) : 1), 0)
  return toTreeNodes(root, '')
}
