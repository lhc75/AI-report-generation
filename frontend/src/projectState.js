export function isProjectBusy(project, chapters = []) {
  return Boolean(project?.operation_token)
    || ['analyzing', 'generating', 'assembling'].includes(project?.status)
    || chapters.some((chapter) => ['queued', 'generating'].includes(chapter.status))
}

export function summarizeOperation(project, chapters, operation = {}) {
  if (project.error_msg || project.status === 'failed') {
    return { type: 'error', message: project.error_msg || '处理失败，请查看失败原因' }
  }
  if (operation.kind === 'assemble') {
    return project.report_path
      ? { type: 'success', message: '报告导出完成，可下载报告' }
      : { type: 'error', message: '导出结束但未生成报告，请重新导出' }
  }
  if (operation.kind === 'analyze') {
    return { type: 'success', message: '模版分析完成' }
  }
  if (operation.runId && project.last_run_id === operation.runId && project.last_run_result) {
    const result = project.last_run_result
    return {
      type: result.failed ? (result.succeeded ? 'warning' : 'error') : 'success',
      message: result.message || `生成结束：成功 ${result.succeeded}，失败 ${result.failed}`,
    }
  }
  const targetIds = operation.taskIds && new Set(operation.taskIds.map(String))
  const tasks = chapters.filter((chapter) => chapter.task_type !== 'heading'
    && (!targetIds || targetIds.has(String(chapter.id))))
  const failed = tasks.filter((task) => task.status === 'failed').length
  const done = tasks.filter((task) => task.status === 'done' && !task.is_stale).length
  const unfinished = tasks.length - failed - done
  if (failed || unfinished) {
    return {
      type: done ? 'warning' : 'error',
      message: `生成结束：成功 ${done}，失败 ${failed}${unfinished ? `，未完成 ${unfinished}` : ''}；可查看历史和失败原因`,
    }
  }
  return { type: 'success', message: tasks.length ? `生成完成：成功 ${done}` : '处理完成，没有待生成的模块' }
}

export function statisticsRows(sheets, config) {
  return sheets.flatMap((sheet) => sheet.columns.map((column) => {
    const field = (config?.fields || []).find((item) => item.sheet === sheet.name
      && item.column_index === column.index && !item.question_key)
    return {
      sheet: sheet.name,
      column_index: column.index,
      column_name: column.name,
      question_type: field?.question_type || '',
      separator: field?.separator || '/',
    }
  }))
}

export function statisticsConfig(rows, preservedFields = []) {
  return {
    fields: [...preservedFields, ...rows.filter((row) => row.question_type).map((row) => ({
      sheet: row.sheet,
      column_index: row.column_index,
      question_type: row.question_type,
      ...(row.question_type === 'multiple' ? { separator: row.separator } : {}),
    }))],
  }
}
