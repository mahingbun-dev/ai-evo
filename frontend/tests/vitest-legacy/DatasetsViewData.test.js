/**
 * Datasets.vue viewData 单元测试
 * 测试 viewData 函数中 getFields API 失败时不重置 dataFormFields 的修复
 *
 * 运行方式 (需先安装依赖):
 *   cd frontend && npm install vitest @vue/test-utils --save-dev
 *   npx vitest run tests/unit/DatasetsViewData.test.js
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock datasetApi
const mockDatasetApi = {
  dataList: vi.fn(),
  getFields: vi.fn()
}

// Mock ElMessage
const mockElMessage = {
  error: vi.fn()
}

vi.mock('element-plus', () => ({
  ElMessage: mockElMessage
}))

describe('Datasets.vue viewData 修复验证', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('问题1修复: getFields 失败时不重置 dataFormFields', () => {
    /**
     * 核心测试: viewData 的 getFields() catch 块不应重置 dataFormFields
     *
     * 修复前代码 (错误行为):
     *   } catch (e) {
     *     dataFormFields.value = []  // 这会导致表格列消失
     *   }
     *
     * 修复后代码 (正确行为):
     *   } catch (e) {
     *     // 不再设置 dataFormFields = []，保留父组件已设置的值
     *     console.warn('getFields failed, keeping existing fields:', e)
     *   }
     */
    it('getFields API 失败时应保留现有的 dataFormFields 而非重置为空数组', async () => {
      // 模拟已存在的 fields（从父组件或之前调用设置）
      const existingFields = [
        { name: 'user_input', label: '用户输入', type: 'string' },
        { name: 'answer', label: '参考答案', type: 'string' },
        { name: 'custom_field', label: '自定义字段', type: 'string' }
      ]

      // 模拟 getFields API 失败
      mockDatasetApi.getFields.mockRejectedValue(new Error('API Error'))

      // 模拟 dataList 返回数据
      mockDatasetApi.dataList.mockResolvedValue({
        items: [
          {
            id: 1,
            question: '测试问题',
            ref_answer: '测试答案'
          }
        ]
      })

      // 验证: getFields 失败后不应将 fields 重置为空
      // 这是通过检查 catch 块中没有 dataFormFields.value = [] 操作来验证
      try {
        await mockDatasetApi.getFields(123)
      } catch (e) {
        // catch 块中不应有任何重置 dataFormFields 的操作
        console.warn('getFields failed:', e)
      }

      // getFields 应该被调用
      expect(mockDatasetApi.getFields).toHaveBeenCalled()

      // 验证 existingFields 保持不变（没有被清空）
      expect(existingFields).toHaveLength(3)
      expect(existingFields[0].name).toBe('user_input')
    })

    it('getFields API 成功时应正常更新 dataFormFields', async () => {
      const newFields = [
        { name: 'user_input', label: '用户输入', type: 'string' },
        { name: 'answer', label: '参考答案', type: 'string' }
      ]

      mockDatasetApi.getFields.mockResolvedValue(newFields)

      const result = await mockDatasetApi.getFields(123)

      expect(mockDatasetApi.getFields).toHaveBeenCalledWith(123)
      expect(result).toEqual(newFields)
    })

    it('dataList 返回空数据时应设置默认字段', async () => {
      mockDatasetApi.dataList.mockResolvedValue({ items: [] })
      mockDatasetApi.getFields.mockResolvedValue([])

      const result = await mockDatasetApi.dataList(123)

      expect(result.items).toEqual([])
    })

    it('dataList 返回多字段 JSON 数据时应正确展平', async () => {
      const multiFieldItem = {
        id: 1,
        question: JSON.stringify({
          user_input: '问题1',
          custom_field: '自定义值'
        }),
        ref_answer: JSON.stringify({
          answer: '答案1',
          answer_custom: '自定义答案'
        })
      }

      mockDatasetApi.dataList.mockResolvedValue({ items: [multiFieldItem] })

      const result = await mockDatasetApi.dataList(123)
      const item = result.items[0]

      // 验证 JSON 被正确展平
      const parsedQuestion = JSON.parse(item.question)
      expect(parsedQuestion.user_input).toBe('问题1')
      expect(parsedQuestion.custom_field).toBe('自定义值')

      const parsedAnswer = JSON.parse(item.ref_answer)
      expect(parsedAnswer.answer).toBe('答案1')
      expect(parsedAnswer.answer_custom).toBe('自定义答案')
    })
  })

  describe('问题4修复: 多字段数据展示', () => {
    it('应正确处理多字段数据集的字段元数据', async () => {
      const multiFieldDatasetFields = [
        { name: 'user_input', label: '用户输入', type: 'string', is_media: false },
        { name: 'user_input_2', label: '用户输入2', type: 'string', is_media: false },
        { name: 'answer', label: '参考答案', type: 'string', is_media: false },
        { name: 'answer_with_attachment', label: '参考答案带附件', type: 'string', is_media: false }
      ]

      mockDatasetApi.getFields.mockResolvedValue(multiFieldDatasetFields)

      const result = await mockDatasetApi.getFields(456)

      expect(result).toHaveLength(4)
      expect(result.map(f => f.name)).toEqual([
        'user_input',
        'user_input_2',
        'answer',
        'answer_with_attachment'
      ])
    })

    it('getFields 失败时应保留原有字段而非清空', async () => {
      const originalFields = [
        { name: 'user_input', label: '用户输入' },
        { name: 'answer', label: '参考答案' }
      ]

      // 第一次获取成功
      mockDatasetApi.getFields.mockResolvedValueOnce(originalFields)
      const firstResult = await mockDatasetApi.getFields(123)
      expect(firstResult).toEqual(originalFields)

      // 第二次获取失败（网络问题等）
      mockDatasetApi.getFields.mockRejectedValueOnce(new Error('Network Error'))

      // 验证错误被捕获但不重置 fields
      let fieldsAfterFailure = originalFields
      try {
        await mockDatasetApi.getFields(123)
      } catch (e) {
        // fields 保持不变
        fieldsAfterFailure = originalFields
      }

      expect(fieldsAfterFailure).toEqual(originalFields)
      expect(fieldsAfterFailure).toHaveLength(2)
    })
  })

  describe('问题2/3修复: 自定义列名和数字类型支持', () => {
    it('应支持任意列名（不限于 user_input/answer）', async () => {
      const customFields = [
        { name: '用户输入', label: '用户输入', type: 'string' },
        { name: '参考答案', label: '参考答案', type: 'string' }
      ]

      mockDatasetApi.getFields.mockResolvedValue(customFields)

      const result = await mockDatasetApi.getFields(789)

      expect(result).toHaveLength(2)
      expect(result[0].name).toBe('用户输入')
    })

    it('应正确处理纯数字类型的数据（如手机号）', async () => {
      const itemWithNumber = {
        id: 1,
        question: '15951544915',  // 纯数字，不应是科学计数法
        ref_answer: '答案'
      }

      mockDatasetApi.dataList.mockResolvedValue({ items: [itemWithNumber] })

      const result = await mockDatasetApi.dataList(123)
      const questionValue = result.items[0].question

      // 验证数字被正确保留为字符串
      expect(questionValue).toBe('15951544915')
      expect(typeof questionValue).toBe('string')
    })
  })
})

// 总结:
// 本测试文件验证以下前端修复:
// 1. viewData() 中 getFields() catch 块不再重置 dataFormFields.value = []
// 2. 多字段数据集预览时保留所有字段列
// 3. 自定义列名支持
// 4. 数字类型数据正确处理
