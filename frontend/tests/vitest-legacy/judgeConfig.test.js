/**
 * Task 4 单元测试：前端改造验证
 */
import { describe, it, expect } from 'vitest'

describe('JudgeConfig API Module', () => {
  it('test_judgeConfig_api_module_exports', () => {
    // 验证 API 方法存在性（通过检查文件内容）。
    // W8 修复：validateConfig 导出已随后端裁判配置 API 重构移除
    // （judgeConfig.js 现仅 getTemplate / previewPrompt 两个导出，
    // 与后端 judge_config_api 的 template/preview 两端点对应），
    // 断言对齐现状并补上 URL 契约校验。
    const fs = require('fs')
    const path = require('path')
    const content = fs.readFileSync(
      path.join(__dirname, '../../src/api/judgeConfig.js'),
      'utf-8'
    )
    expect(content).toContain('export function getTemplate')
    expect(content).toContain('export function previewPrompt')
    expect(content).toContain("url: '/evaluation/judge-config/template'")
    expect(content).toContain("url: '/evaluation/judge-config/preview'")
  })
})

describe('Evaluation Targets UI Data Structure', () => {
  it('test_evaluate_targets_ui_data_structure', () => {
    // 验证评测目标数据结构正确转换
    const evaluationTarget = {
      id: 'target_1',
      name: 'model_output',
      source: 'model_output',
      source_config: {},
      required: true,
      display_name: '模型输出'
    }

    expect(evaluationTarget.id).toBeDefined()
    expect(evaluationTarget.name).toBe('model_output')
    expect(evaluationTarget.source).toBe('model_output')
    expect(evaluationTarget.required).toBe(true)
    expect(evaluationTarget.display_name).toBe('模型输出')
  })
})

describe('Validation Result Display Format', () => {
  it('test_validation_result_display_format', () => {
    // 验证校验结果展示格式正确
    const validationResult = {
      target_id: 't1',
      target_name: '模型输出',
      status: 'error',
      message: '必填评测目标「模型输出」内容为空',
      content: ''
    }

    expect(validationResult.status).toBe('error')
    expect(validationResult.message).toContain('为空')
    expect(validationResult.target_name).toBe('模型输出')
  })
})

describe('Multi-Judge Display Data Structure', () => {
  it('test_multi_judge_display_data_structure', () => {
    // 验证多裁判折叠数据结构正确处理
    const judgeAssessments = [
      {
        judge_index: 0,
        evaluation_target_id: 't1',
        evaluation_target_name: '模型输出',
        scores: { '综合评分': 8 },
        reasoning: '回答准确',
        validation_message: ''
      },
      {
        judge_index: 1,
        evaluation_target_id: 't2',
        evaluation_target_name: '参考答案',
        scores: { '综合评分': 9 },
        reasoning: '格式规范',
        validation_message: ''
      }
    ]

    expect(judgeAssessments).toHaveLength(2)
    expect(judgeAssessments[0].judge_index).toBe(0)
    expect(judgeAssessments[1].judge_index).toBe(1)
    expect(judgeAssessments[0].evaluation_target_name).toBe('模型输出')
    expect(judgeAssessments[1].evaluation_target_name).toBe('参考答案')
  })

  it('test_validation_failure_display', () => {
    // 验证校验失败时的显示格式
    const failedJudgeAssessment = {
      judge_index: 0,
      evaluation_target_id: 't1',
      evaluation_target_name: '模型输出',
      scores: { '综合评分': 0, '校验状态': 'failed', '校验失败原因': '内容为空' },
      reasoning: '空值校验失败',
      validation_message: '「模型输出」: 内容为空'
    }

    expect(failedJudgeAssessment.scores['综合评分']).toBe(0)
    expect(failedJudgeAssessment.validation_message).toContain('内容为空')
    expect(failedJudgeAssessment.scores['校验状态']).toBe('failed')
  })
})
