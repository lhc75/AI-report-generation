import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createProjectPoller } from '../src/projectPolling.js'
import { isProjectBusy, statisticsConfig, statisticsRows, summarizeOperation } from '../src/projectState.js'

const source = readFileSync(new URL('../src/views/ProjectDetail.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const bindings = Object.keys(compileScript(descriptor, { id: 'project-test' }).bindings)
const compiled = compileTemplate({ source: descriptor.template.content, filename: 'ProjectDetail.vue', id: 'project-test' })
assert.deepEqual(compiled.errors, [])
const render = new Function('Vue', compiled.code
  .replace(/import \{([^}]+)\} from "vue"/, (_, names) => `const {${names.replace(/ as /g, ': ')}} = Vue`)
  .replace('export function render', 'function render') + '\nreturn render')(Vue)

function fakeTimers() {
  const timers = new Map()
  let id = 0
  return {
    setTimer(fn, delay) { timers.set(++id, { fn, delay }); return id },
    clearTimer(key) { timers.delete(key) },
    get size() { return timers.size },
    get delay() { return [...timers.values()][0]?.delay },
    async next() {
      const [key, timer] = [...timers][0] || []
      assert.ok(timer, 'an expected polling timer exists')
      timers.delete(key)
      await timer.fn()
    },
  }
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function harness(customApi = {}) {
  const timers = fakeTimers()
  const calls = []
  const messages = []
  const unmount = []
  const api = {
    get: async (url, options) => {
      calls.push({ method: 'get', url, options })
      if (customApi.get) return customApi.get(url, options)
      if (url.endsWith('/chapters')) return { data: [] }
      if (url.endsWith('/files')) return { data: [] }
      return { data: { id: 1, status: 'analyzed', operation_token: '', error_msg: '', report_path: '' } }
    },
    post: async (url, body) => { calls.push({ method: 'post', url, body }); return { data: { run_id: 'run-1' } } },
    patch: async (url, body, options) => {
      calls.push({ method: 'patch', url, body, options })
      return customApi.patch ? customApi.patch(url, body, options) : { data: { ok: true } }
    },
    put: async (url, body) => { calls.push({ method: 'put', url, body }); return { data: {} } },
    delete: async (url) => { calls.push({ method: 'delete', url }); return { data: {} } },
  }
  const context = vm.createContext({
    computed: Vue.computed, ref: Vue.ref, nextTick: Vue.nextTick,
    onMounted: () => {}, onBeforeUnmount: (callback) => unmount.push(callback),
    useRoute: () => ({ params: { id: 1 } }),
    ElMessage: Object.fromEntries(['warning', 'error', 'success'].map((type) => [type, (message) => messages.push({ type, message })])),
    ElMessageBox: { confirm: async () => {} },
    Back: {}, Delete: {}, Document: {}, Download: {}, FolderChecked: {}, MagicStick: {}, Promotion: {}, UploadFilled: {}, View: {},
    api, isProjectBusy, statisticsConfig, statisticsRows, summarizeOperation,
    renderAsync: customApi.renderAsync || (async () => {}),
    createProjectPoller: (options) => createProjectPoller({ ...options, setTimer: timers.setTimer, clearTimer: timers.clearTimer }),
    AbortController, FormData, Blob,
  })
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
    + `\nglobalThis.model = {${bindings.join(',')}}`, context)
  const model = context.model
  model.project.value = { id: 1, status: 'analyzed', operation_token: '', error_msg: '', report_path: '' }
  model.pageLoading.value = false
  return { model, api, calls, messages, timers, unmount: () => unmount.forEach((callback) => callback()) }
}

function simpleComponent(name, tag = 'div') {
  return Vue.defineComponent({
    name, props: ['disabled', 'modelValue'],
    setup(props, { slots }) {
      return () => name === 'ElDialog' && !props.modelValue ? null
        : Vue.h(tag, { disabled: props.disabled || undefined }, [slots.title?.(), slots.default?.(), slots.footer?.()])
    },
  })
}
function collectColumns(nodes = []) {
  return nodes.flatMap((node) => {
    if (node.type?.name === 'ElTableColumn') return [node]
    return Array.isArray(node.children) ? collectColumns(node.children) : []
  })
}
async function htmlFor(model) {
  const app = Vue.createSSRApp({ setup: () => model, render })
  for (const name of ['ElButton', 'ElUpload', 'ElRow', 'ElCol', 'ElIcon', 'ElTag', 'ElAlert', 'ElTooltip', 'ElDialog', 'ElSelect', 'ElOption', 'ElInput', 'ElEmpty', 'ElTableColumn', 'ElForm', 'ElFormItem', 'Document']) {
    app.component(name, simpleComponent(name, name === 'ElButton' ? 'button' : 'div'))
  }
  app.component('ElTable', Vue.defineComponent({
    name: 'ElTable', props: ['data'],
    setup(props, { slots }) {
      return () => Vue.h('table', (props.data || []).map((row) => Vue.h('tr', collectColumns(slots.default?.()).map((column) =>
        Vue.h('td', column.children?.default?.({ row }) || String(row[column.props?.prop] ?? ''))))))
    },
  }))
  app.directive('loading', {})
  return renderToString(app)
}

test('history remains accessible with no current version and failed latest content', async () => {
  const history = [{ id: 2, version: 2, status: 'failed' }, { id: 1, version: 1, status: 'ok' }]
  const setup = harness({ get: async (url, options) => ({ data: {
    versions: history,
    content: options.params.content_id === 1
      ? { id: 1, status: 'ok', is_current: false, content_type: 'text', content: 'old successful content' }
      : { id: 2, status: 'failed', error_msg: 'provider timeout', is_current: false },
  } }) })
  const row = { id: 1, task_type: 'text', status: 'failed', has_content: false, history_count: 2 }
  setup.model.chapters.value = [row]
  assert.match(await htmlFor(setup.model), /内容与历史/)
  await setup.model.openContent(row)
  assert.equal(setup.model.versions.value.length, 2)
  assert.match(await htmlFor(setup.model), /provider timeout/)
  setup.model.contentDetail.value = null
  assert.match(await htmlFor(setup.model), /v2/)
  await setup.model.openContent(row, 1)
  assert.match(await htmlFor(setup.model), /old successful content/)
  assert.match(await htmlFor(setup.model), /回退到此版本/)
  setup.unmount()
})

test('current text and table render the report display copy while history keeps its source', async () => {
  const setup = harness()
  setup.model.contentVisible.value = true
  setup.model.contentTask.value = { id: 1 }
  setup.model.contentDetail.value = {
    id: 1, status: 'ok', is_current: true, content_type: 'text',
    content: '原存档正文及重复核验说明', display_content: '本题作答237条，选择116条（48.9%）。',
  }
  let html = await htmlFor(setup.model)
  assert.match(html, /本题作答237条，选择116条（48.9%）。/)
  assert.doesNotMatch(html, /原存档正文及重复核验说明/)
  const rows = [['其他', 116, '48.9%']]
  setup.model.contentDetail.value = {
    id: 2, status: 'ok', is_current: true, content_type: 'table',
    content: JSON.stringify({ columns: ['原列名'], rows, note: '重复核验说明' }),
    display_content: JSON.stringify({ columns: ['选项', '记录数', '比例'], rows, note: '参与资格尚待核实。' }),
  }
  html = await htmlFor(setup.model)
  assert.equal(setup.model.tableColumns.value.join('、'), '选项、记录数、比例')
  assert.match(html, /116/)
  assert.match(html, /48.9%/)
  assert.match(html, /参与资格尚待核实。/)
  assert.doesNotMatch(html, /重复核验说明/)
  setup.model.contentDetail.value = {
    id: 3, status: 'ok', is_current: false, content_type: 'text', content: '历史原始核验说明',
  }
  assert.match(await htmlFor(setup.model), /历史原始核验说明/)
  setup.model.contentDetail.value = {
    id: 4, status: 'ok', content_type: 'text', content: '原正文', display_content: '',
  }
  assert.equal(setup.model.displayedContent.value, '', 'an intentionally empty display copy must not fall back')
  setup.unmount()
})

test('stale history is readable but cannot be rolled back after materials or instructions change', async () => {
  const setup = harness()
  setup.model.contentVisible.value = true
  setup.model.contentTask.value = { id: 1 }
  setup.model.versions.value = [{ id: 1, version: 1, status: 'ok', is_stale: true }]
  setup.model.contentDetail.value = {
    id: 1, status: 'ok', is_current: false, can_rollback: false, is_stale: true,
    content_type: 'text', content: 'previous successful content',
  }
  const html = await htmlFor(setup.model)
  assert.match(html, /previous successful content/)
  assert.match(html, /资料或生成指令已更新，该版本仅可查看，不能回退。/)
  assert.match(html.replace(/<!--[\s\S]*?-->/g, ''), /<button[^>]*disabled[^>]*>回退到此版本<\/button>/)
  assert.match(html, /（待更新）/)
  await setup.model.rollback()
  assert.equal(setup.calls.length, 0, 'the rollback action also rejects unavailable versions')
  setup.unmount()
})

test('every mutation is guarded while operation token or any chapter is busy; history stays readable', async () => {
  for (const busy of [{ operation_token: 'another-run' }, { status: 'analyzing' }, { status: 'generating' }, { status: 'assembling' }, { chapter: 'queued' }, { chapter: 'generating' }]) {
    const setup = harness({ get: async () => ({ data: { versions: [], content: null } }) })
    Object.assign(setup.model.project.value, busy)
    const row = { id: 1, task_type: 'text', status: busy.chapter || 'done', has_content: true, history_count: 1 }
    setup.model.chapters.value = [row]
    setup.model.rewriteTask.value = row
    setup.model.insertAfter.value = row
    setup.model.insertRequirement.value = 'new content'
    setup.model.contentTask.value = row
    setup.model.contentDetail.value = { id: 1, status: 'ok' }
    setup.model.statisticsFile.value = { id: 3 }
    setup.model.editTask.value = row
    setup.model.editTitle.value = '标题'
    setup.model.editInstruction.value = '生成内容'
    setup.model.openEdit(row)
    assert.equal(setup.model.editVisible.value, false, 'busy rows cannot open editing')
    await setup.model.analyze()
    await setup.model.generateAll()
    await setup.model.generateOne(row)
    await setup.model.doRewrite()
    await setup.model.doInsert()
    await setup.model.removeTask(row)
    await setup.model.removeFile({ id: 3 })
    await setup.model.rollback()
    await setup.model.saveStatistics()
    await setup.model.saveEdit()
    await setup.model.assembleProject()
    await assert.rejects(setup.model.upload('data', { file: new Blob(['data']) }))
    assert.equal(setup.calls.length, 0, `all writes are blocked for ${JSON.stringify(busy)}`)
    await setup.model.openContent(row)
    assert.equal(setup.calls.length, 1, 'history read is permitted')
    setup.unmount()
  }
})

test('busy controls including already open dialogs are disabled while the history button stays enabled', async () => {
  const setup = harness()
  setup.model.project.value.operation_token = 'another-run'
  const row = { id: 1, task_type: 'text', status: 'done', has_content: true, history_count: 1 }
  setup.model.chapters.value = [row]
  setup.model.rewriteVisible.value = true
  setup.model.insertVisible.value = true
  setup.model.contentVisible.value = true
  setup.model.statisticsVisible.value = true
  setup.model.editVisible.value = true
  setup.model.editTask.value = row
  setup.model.versions.value = [{ id: 1, version: 1 }]
  setup.model.contentDetail.value = { id: 1, status: 'ok', is_current: false }
  const html = await htmlFor(setup.model)
  const buttons = [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].map((match) => ({
    disabled: match[1].includes('disabled'),
    label: match[2].replace(/<[^>]+>/g, '').trim(),
  }))
  for (const label of ['导出报告', '全部生成', '分析', '上传模版', '上传参考资料', '上传数据', '编辑', '重写', '插入', '删除', '保存修改', '开始重写', '插入并生成', '回退到此版本', '保存设置']) {
    assert.equal(buttons.find((button) => button.label === label)?.disabled, true, `${label} is disabled`)
  }
  assert.equal(buttons.find((button) => button.label === '内容与历史')?.disabled, false)
  setup.unmount()
})

test('heading and module rows offer editing; opening and cancelling preserve task fields', async () => {
  const setup = harness()
  const heading = { id: 2, task_type: 'heading', title: '调查说明' }
  const row = { id: 7, task_type: 'text', title: '调查时间', instruction: '依据提交时间说明实际调查时间', status: 'pending' }
  setup.model.chapters.value = [heading, row]
  const buttons = [...(await htmlFor(setup.model)).matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)]
  assert.equal(buttons.filter((match) => match[1].replace(/<[^>]+>/g, '').trim() === '编辑').length, 2)
  setup.model.openEdit(row)
  assert.equal(setup.model.editVisible.value, true)
  assert.equal(setup.model.editTitle.value, row.title)
  assert.equal(setup.model.editInstruction.value, row.instruction)
  assert.match(await htmlFor(setup.model), /已有内容和历史版本会保留/)
  setup.model.editTitle.value = '临时修改'
  setup.model.editInstruction.value = '临时指令'
  setup.model.cancelEdit()
  assert.equal(setup.model.editVisible.value, false)
  assert.equal(row.title, '调查时间')
  assert.equal(row.instruction, '依据提交时间说明实际调查时间')
  assert.equal(setup.calls.length, 0)
  setup.model.openEdit(heading)
  assert.equal(setup.model.editTitle.value, heading.title)
  const headingHtml = await htmlFor(setup.model)
  assert.match(headingHtml, /编辑章节标题/)
  assert.doesNotMatch(headingHtml, /生成指令/)
  setup.unmount()
})

test('editing validates nonempty title and generation instruction before sending changes', async () => {
  const setup = harness()
  setup.model.openEdit({ id: 7, task_type: 'text', title: '模块', instruction: '原指令' })
  setup.model.editTitle.value = '  '
  await setup.model.saveEdit()
  assert.equal(setup.model.editError.value, '请填写标题')
  setup.model.editTitle.value = '修改后的模块'
  setup.model.editInstruction.value = '\n  '
  await setup.model.saveEdit()
  assert.equal(setup.model.editError.value, '请填写生成指令')
  assert.equal(setup.model.editVisible.value, true)
  assert.equal(setup.calls.length, 0)
  setup.unmount()
})

test('saving edits sends trimmed fields and refreshes immediately without starting generation', async () => {
  for (const taskType of ['heading', 'text']) {
    const setup = harness()
    setup.model.openEdit({ id: 7, task_type: taskType, title: '原标题', instruction: '原指令' })
    setup.model.editTitle.value = '  更新标题  '
    setup.model.editInstruction.value = '  生成表格：各地区人数  '
    await setup.model.saveEdit()
    assert.equal(setup.calls[0].method, 'patch')
    assert.equal(setup.calls[0].url, '/projects/1/tasks/7')
    assert.deepEqual(JSON.parse(JSON.stringify(setup.calls[0].body)), taskType === 'heading'
      ? { title: '更新标题' } : { title: '更新标题', instruction: '生成表格：各地区人数' })
    assert.equal(setup.calls[0].options.silentError, true)
    assert.deepEqual(setup.calls.slice(1).map((call) => call.url), [
      '/projects/1', '/projects/1/chapters', '/projects/1/files',
    ])
    assert.equal(setup.model.editVisible.value, false)
    assert.equal(setup.model.activeOperation.value, null)
    assert.equal(setup.timers.size, 0)
    assert.match(setup.messages.at(-1).message, taskType === 'heading' ? /重新导出/ : /重新生成/)
    setup.unmount()
  }
})

test('failed edits preserve dialog input and refresh server state after a conflict', async () => {
  const setup = harness({ patch: async () => { throw { response: { status: 409, data: { detail: '项目正在生成' } } } } })
  setup.model.openEdit({ id: 7, task_type: 'text', title: '原标题', instruction: '原指令' })
  setup.model.editTitle.value = '更新标题'
  setup.model.editInstruction.value = '更新指令'
  await setup.model.saveEdit()
  assert.equal(setup.model.editVisible.value, true)
  assert.equal(setup.model.editTitle.value, '更新标题')
  assert.equal(setup.model.editInstruction.value, '更新指令')
  assert.equal(setup.model.editError.value, '保存失败：项目正在生成')
  assert.equal(setup.model.acting.value, false)
  assert.match(await htmlFor(setup.model), /保存失败：项目正在生成/)
  assert.deepEqual(setup.calls.map((call) => call.url), [
    '/projects/1/tasks/7', '/projects/1', '/projects/1/chapters', '/projects/1/files',
  ])
  assert.equal(setup.messages.filter((message) => message.type === 'success').length, 0)
  setup.unmount()
})

test('validation errors from the edit API render readable messages and preserve unsaved input', async () => {
  const setup = harness({ patch: async () => { throw { response: { status: 422, data: { detail: [
    { loc: ['body', 'title'], msg: '标题不能超过512个字符' },
    { loc: ['body', 'instruction'], msg: '生成指令不能超过20000个字符' },
  ] } } } } })
  setup.model.openEdit({ id: 7, task_type: 'text', title: '原标题', instruction: '原指令' })
  setup.model.editTitle.value = '保存中的标题'
  setup.model.editInstruction.value = '保存中的指令'
  await setup.model.saveEdit()
  assert.equal(setup.model.editVisible.value, true)
  assert.equal(setup.model.editTitle.value, '保存中的标题')
  assert.equal(setup.model.editInstruction.value, '保存中的指令')
  assert.equal(setup.model.editError.value, '保存失败：标题不能超过512个字符；生成指令不能超过20000个字符')
  assert.doesNotMatch(await htmlFor(setup.model), /\[object Object\]/)
  assert.equal(setup.messages.filter((message) => message.type === 'success').length, 0)
  assert.equal(setup.model.acting.value, false)
  setup.unmount()
})

test('task rows show full instructions without repeating titles inferred from their prefix', async () => {
  const setup = harness()
  const instruction = '依据各工作表的提交时间、有效回答人数和问卷名称核对调查对象，说明各地区实际调查时间，逐项检查数据完整性并写出结论。'
  for (const title of [instruction, instruction.slice(0, 60), `${instruction.slice(0, 30)}…`]) {
    const row = { id: 7, task_type: 'text', title, instruction, status: 'pending' }
    const display = setup.model.taskDisplay(row)
    assert.equal(display.primary, instruction)
    assert.equal(display.secondary, '')
    assert.equal(display.tooltip, instruction)
    setup.model.chapters.value = [row]
    const html = await htmlFor(setup.model)
    assert.match(html, /逐项检查数据完整性并写出结论。/)
    assert.doesNotMatch(html, /class="task-instruction"/)
  }
  const display = setup.model.taskDisplay({ task_type: 'heading', title: '调查说明', instruction: '隐藏的指令' })
  assert.equal(display.primary, '调查说明')
  assert.equal(display.secondary, '')
  assert.equal(display.tooltip, '调查说明')
  setup.unmount()
})

test('instruction-only edits become visible with their distinct title after the save refresh', async () => {
  const updated = { id: 7, task_type: 'text', title: '调查时间', instruction: '依据更新后的提交时间说明完整调查周期', status: 'pending', is_stale: true }
  const setup = harness({ get: async (url) => ({ data: url.endsWith('/chapters') ? [updated]
    : url.endsWith('/files') ? [] : { id: 1, status: 'analyzed', operation_token: '' } }) })
  setup.model.openEdit({ ...updated, instruction: '旧生成指令' })
  setup.model.editInstruction.value = updated.instruction
  await setup.model.saveEdit()
  assert.equal(setup.calls[0].body.title, '调查时间')
  assert.equal(setup.calls[0].body.instruction, updated.instruction)
  const display = setup.model.taskDisplay(setup.model.chapters.value[0])
  assert.equal(display.primary, '调查时间')
  assert.equal(display.secondary, updated.instruction)
  assert.equal(display.tooltip, `调查时间\n${updated.instruction}`)
  const html = await htmlFor(setup.model)
  assert.match(html, /class="task-instruction"[^>]*>依据更新后的提交时间说明完整调查周期/)
  assert.match(html, /生成指令或内容已更新，旧内容已保留；请重新生成待更新模块后导出。/)
  assert.doesNotMatch(html, /请重新分析/)
  setup.model.project.value.status = 'created'
  const materialsHtml = await htmlFor(setup.model)
  assert.match(materialsHtml, /资料或统计设置已变更，旧内容已保留；请重新分析并生成后导出。/)
  assert.doesNotMatch(materialsHtml, /生成指令或内容已更新/)
  setup.unmount()
})

test('task mutation refreshes project, chapters and files immediately and tracks run id', async () => {
  const setup = harness()
  await setup.model.generateOne({ id: 7 })
  assert.equal(setup.model.activeOperation.value.runId, 'run-1')
  assert.deepEqual(setup.calls.map((call) => call.url), [
    '/projects/1/tasks/7/generate', '/projects/1', '/projects/1/chapters', '/projects/1/files',
  ])
  setup.unmount()
})

test('a new mutation settles the previous snapshot and respects a newly discovered busy run', async () => {
  const pendingProject = deferred()
  const setup = harness({ get: (url) => url === '/projects/1' ? pendingProject.promise : Promise.resolve({ data: [] }) })
  const previousRefresh = setup.model.refreshSnapshot()
  const mutation = setup.model.generateOne({ id: 7 })
  assert.equal(setup.calls.filter((call) => call.method === 'post').length, 0)
  pendingProject.resolve({ data: { id: 1, status: 'generating', operation_token: 'other-browser' } })
  await previousRefresh
  await mutation
  assert.equal(setup.calls.filter((call) => call.method === 'post').length, 0, 'no write is sent after discovering another run')
  assert.equal(setup.model.mutationBlocked.value, true)
  setup.unmount()
})

test('all failed runs use an error message and matching run result excludes old failures', () => {
  const chapters = [{ id: 1, task_type: 'text', status: 'failed' }]
  assert.equal(summarizeOperation({ status: 'analyzed' }, chapters, { kind: 'generate' }).type, 'error')
  assert.equal(summarizeOperation({ status: 'analyzed', last_run_id: 'r2', last_run_result: { succeeded: 1, failed: 0, total: 1 } }, chapters,
    { kind: 'generate', runId: 'r2' }).type, 'success')
  assert.equal(summarizeOperation({ status: 'analyzed', error_msg: 'export failed', report_path: 'old-report' }, [], { kind: 'assemble' }).type, 'error')
})

test('load failures always clear initial loading and present a retryable error', async () => {
  const setup = harness({ get: async () => { throw new Error('network offline') } })
  setup.model.pageLoading.value = true
  await setup.model.loadPage()
  assert.equal(setup.model.pageLoading.value, false)
  assert.equal(setup.model.chaptersLoading.value, false)
  assert.match(setup.model.loadError.value, /network offline/)
  assert.equal(setup.model.mutationBlocked.value, true)
  setup.unmount()
})

test('switching history discards an older response and cancels the previous request', async () => {
  const first = deferred()
  const setup = harness({ get: (url, options) => options.params.content_id === 1 ? first.promise
    : Promise.resolve({ data: { versions: [{ id: 2 }], content: { id: 2, status: 'ok', content: 'new selection' } } }) })
  const row = { id: 1 }
  const older = setup.model.openContent(row, 1)
  const signal = setup.calls[0].options.signal
  await setup.model.openContent(row, 2)
  assert.equal(signal.aborted, true)
  first.resolve({ data: { versions: [{ id: 1 }], content: { id: 1, content: 'late response' } } })
  await older
  assert.equal(setup.model.contentDetail.value.id, 2)
  setup.unmount()
})

test('statistics settings preserve indexed columns and submit only explicit overrides', async () => {
  const sheets = [{ name: '问卷', columns: [{ index: 0, name: '选项' }, { index: 1, name: '选项' }] }]
  const rows = statisticsRows(sheets, { fields: [{ sheet: '问卷', column_index: 1, question_type: 'multiple', separator: ';' }] })
  assert.deepEqual(statisticsConfig(rows).fields, [{ sheet: '问卷', column_index: 1, question_type: 'multiple', separator: ';' }])
  rows[1].question_type = ''
  assert.deepEqual(statisticsConfig(rows), { fields: [] })
  rows[0].question_type = 'single'
  assert.deepEqual(statisticsConfig(rows).fields, [{ sheet: '问卷', column_index: 0, question_type: 'single' }],
    'single-choice fields omit separators as required by the API')
  rows[0].question_type = ''
  const packedField = { sheet: '问卷', column_index: 0, question_key: 'q1', question_type: 'multiple', separator: '/', denominator: 'answered' }
  assert.equal(statisticsRows(sheets, { fields: [packedField] })[0].question_type, '', 'a packed question override is not applied to the whole column')
  assert.deepEqual(statisticsConfig(rows, [packedField]).fields, [packedField])
  const setup = harness()
  setup.model.statisticsFile.value = { id: 3 }
  setup.model.statisticsFields.value = rows
  await setup.model.saveStatistics()
  assert.equal(setup.calls[0].url, '/projects/1/files/3/statistics-config')
  assert.deepEqual(setup.calls[0].body, { fields: [] })
  assert.equal(setup.model.statisticsVisible.value, false)
  setup.unmount()
})

test('poller never overlaps slow requests and reschedules only after settlement', async () => {
  const timers = fakeTimers()
  const wait = deferred()
  let reads = 0
  const poller = createProjectPoller({ ...timers,
    read: () => { reads += 1; return wait.promise }, onSnapshot: () => true, onError: () => true,
  })
  poller.start()
  const pending = timers.next()
  poller.start()
  assert.equal(reads, 1)
  assert.equal(timers.size, 0, 'no timer exists while the request is pending')
  wait.resolve({})
  await pending
  assert.equal(timers.size, 1)
  assert.equal(timers.delay, 2000)
  poller.stop()
  assert.equal(timers.size, 0)
})

test('a partially failed snapshot waits for sibling requests before retrying', async () => {
  const sibling = deferred()
  const setup = harness({ get: (url) => url.endsWith('/chapters')
    ? Promise.reject(new Error('temporary failure')) : sibling.promise })
  setup.model.startPolling()
  const pending = setup.timers.next()
  await Promise.resolve()
  assert.equal(setup.timers.size, 0)
  assert.equal(setup.calls.length, 3)
  sibling.resolve({ data: {} })
  await pending
  assert.equal(setup.timers.delay, 4000)
  assert.equal(setup.model.mutationBlocked.value, true)
  setup.unmount()
})

test('poller backs off transient failures, stops terminal errors and cancels unmount reads', async () => {
  const timers = fakeTimers()
  let status = 503
  let errors = 0
  const poller = createProjectPoller({ ...timers,
    read: async () => { throw { response: { status } } },
    onSnapshot: () => true, onError: (error) => { errors += 1; return error.response.status !== 401 },
  })
  poller.start()
  await timers.next()
  assert.equal(timers.delay, 4000)
  await timers.next()
  assert.equal(timers.delay, 8000)
  status = 401
  await timers.next()
  assert.equal(timers.size, 0)
  assert.equal(errors, 3)
  const wait = deferred()
  let signal
  let snapshots = 0
  const pendingPoller = createProjectPoller({ ...timers,
    read: (receivedSignal) => { signal = receivedSignal; return wait.promise },
    onSnapshot: () => { snapshots += 1 }, onError: () => true,
  })
  pendingPoller.start()
  const pending = timers.next()
  pendingPoller.stop()
  assert.equal(signal.aborted, true)
  wait.resolve({})
  await pending
  assert.equal(snapshots, 0)
  assert.equal(timers.size, 0)
})

test('预览报告按钮仅在已导出报告时出现', async () => {
  const exported = harness()
  exported.model.project.value = { id: 1, status: 'analyzed', operation_token: '', error_msg: '', report_path: '1/1/report/final.docx' }
  assert.match(await htmlFor(exported.model), /预览报告/)
  const pending = harness()
  assert.doesNotMatch(await htmlFor(pending.model), /预览报告/)
})

test('点击预览以 blob 拉取 DOCX 并渲染到对话框', async () => {
  let rendered = null
  const fakeBlob = { fake: 'docx-bytes' }
  const setup = harness({
    renderAsync: async (blob, target) => { rendered = { blob, target } },
    get: async (url) => {
      assert.equal(url, '/projects/1/report/preview')
      return { data: fakeBlob }
    },
  })
  setup.model.project.value = { id: 1, status: 'analyzed', operation_token: '', error_msg: '', report_path: '1/1/report/final.docx' }
  await setup.model.previewReport()
  const call = setup.calls.at(-1)
  assert.equal(call.method, 'get')
  assert.equal(call.url, '/projects/1/report/preview')
  assert.equal(call.options.responseType, 'blob')
  assert.equal(call.options.silentError, true)
  assert.equal(setup.model.previewVisible.value, true)
  assert.equal(rendered.blob, fakeBlob)
  assert.equal(setup.model.previewLoading.value, false)
  assert.equal(setup.model.previewError.value, '')
  assert.match(await htmlFor(setup.model), /报告预览/)
})

test('预览失败时解析 Blob 错误详情提示并结束加载态', async () => {
  const errBlob = new Blob([JSON.stringify({ detail: '尚未导出报告，请先点击“导出报告”' })], { type: 'application/json' })
  const setup = harness({
    get: async () => { throw { response: { data: errBlob } } },
  })
  setup.model.project.value = { id: 1, status: 'analyzed', operation_token: '', error_msg: '', report_path: 'stale.docx' }
  await setup.model.previewReport()
  assert.equal(setup.model.previewLoading.value, false)
  assert.equal(setup.model.previewError.value, '尚未导出报告，请先点击“导出报告”')
  assert.deepEqual(setup.messages.at(-1), { type: 'error', message: '尚未导出报告，请先点击“导出报告”' })
  assert.match(await htmlFor(setup.model), /尚未导出报告/)
  setup.model.clearPreview()
  assert.equal(setup.model.previewError.value, '')
})
