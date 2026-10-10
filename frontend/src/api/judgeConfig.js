/**
 * 裁判配置 API 调用模块
 */
import request from './index.js'

/**
 * 获取默认裁判提示词模板结构
 * @returns {Promise}
 */
export function getTemplate() {
  return request({
    url: '/evaluation/judge-config/template',
    method: 'get'
  })
}

/**
 * 预览渲染后的提示词
 * @param {Object} data - 请求数据
 * @param {string} data.system_prompt_template - System prompt Jinja2 模板
 * @param {string} data.user_prompt_template - User prompt Jinja2 模板
 * @param {Array} data.targets - 评测目标列表
 * @param {Object} data.dataset_item - 数据集项
 * @param {Object} data.model_result - 模型结果
 * @returns {Promise}
 */
export function previewPrompt(data) {
  return request({
    url: '/evaluation/judge-config/preview',
    method: 'post',
    data
  })
}

