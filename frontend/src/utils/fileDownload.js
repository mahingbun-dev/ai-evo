/**
 * blob 文件保存工具：触发浏览器下载（与 Datasets.vue/Models.vue 内联实现同模式，抽公共复用）
 */

// 保存 blob 为文件（中文文件名直接写入 download 属性即可，浏览器按 UTF-8 处理）
export function saveBlobFile(blob, filename) {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}
