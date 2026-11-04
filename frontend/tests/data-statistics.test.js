import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import * as Vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const pageSource = readFileSync(new URL('../src/views/DataStatistics.vue', import.meta.url), 'utf8')
const chartSource = readFileSync(new URL('../src/components/DashboardBarChart.vue', import.meta.url), 'utf8')
const layoutSource = readFileSync(new URL('../src/views/Layout.vue', import.meta.url), 'utf8')
const routerSource = readFileSync(new URL('../src/router.js', import.meta.url), 'utf8')

function compilePage() {
  const { descriptor } = parse(pageSource)
  const compiled = compileTemplate({
    source: descriptor.template.content,
    filename: 'DataStatistics.vue',
    id: 'data-statistics-test',
  })
  assert.deepEqual(compiled.errors, [])
  const render = new Function('Vue', compiled.code
    .replace(/import \{([^}]+)\} from "vue"/, (_, names) => `const {${names.replace(/ as /g, ': ')}} = Vue`)
    .replace('export function render', 'function render') + '\nreturn render')(Vue)
  const bindings = Object.keys(compileScript(descriptor, { id: 'data-statistics-test' }).bindings)
  return { descriptor, render, bindings }
}

async function flush(times = 20) {
  for (let index = 0; index < times; index += 1) await Promise.resolve()
}

function harness({ includeBuiltin = true } = {}) {
  const calls = []
  const messages = []
  const router = { push: (path) => calls.push({ method: 'push', path }) }
  const overviewPayload = {
    file: { id: 11, file_name: 'answers.csv', size: 128, created_at: '2026-10-14 12:00:00' },
    summary: {
      total_submissions: 10,
      student_count: 4,
      school_count: 2,
      last_update: '2026-10-03T10:00:00',
    },
    charts: {
      city: [{ name: '济南市', value: 5 }, { name: '青岛市', value: 5 }],
      grade: [{ name: '七年级', value: 7 }, { name: '八年级', value: 3 }],
    },
    school_query: {
      found: true,
      school: '第一中学',
      city: '济南市',
      area: '历下区',
      schoolId: '1001',
      total_submissions: 5,
      student_count: 2,
      grades: [{ name: '七年级', value: 2 }, { name: '八年级', value: 3 }],
    },
  }
  const api = {
    get: async (url, options) => {
      calls.push({ method: 'get', url, options })
      if (url === '/dashboard/sources') {
        const projects = [{ id: 1, name: '统计项目', data_files: [{ id: 11, file_name: 'answers.csv', size: 128, created_at: '2026-10-14 12:00:00' }] }]
        if (includeBuiltin) {
          projects.unshift({ id: 'builtin-student', name: '内置学生作答数据（12.14）', data_files: [{ id: 'builtin:legacy.csv', file_name: 'legacy.csv', size: 19082335, created_at: '2026-10-14 12:00:00' }] })
        }
        return { data: { projects } }
      }
      if (url === '/dashboard/overview') {
        const payload = structuredClone(overviewPayload)
        payload.file.file_name = options?.params?.file_id === 'builtin:legacy.csv' ? 'legacy.csv' : 'answers.csv'
        return { data: payload }
      }
      throw new Error(`unexpected GET ${url}`)
    },
    post: async (url, body, options) => {
      calls.push({ method: 'post', url, body, options })
      if (url === '/dashboard/school-table') {
        return { data: { columns: ['city', 'submit_count'], rows: [{ city: '济南市', submit_count: 5 }], total: 1 } }
      }
      if (url === '/dashboard/records') {
        return { data: {
          columns: ['username', 'city'],
          rows: [{ username: `u${body.page}`, city: '济南市' }],
          page: body.page,
          page_size: body.page_size,
          total: 10,
        } }
      }
      throw new Error(`unexpected POST ${url}`)
    },
  }
  const context = vm.createContext({
    computed: Vue.computed,
    reactive: Vue.reactive,
    ref: Vue.ref,
    useRouter: () => router,
    ElMessage: { warning: (message) => messages.push({ type: 'warning', message }) },
    Clock: {}, DataAnalysis: {}, Delete: {}, Plus: {}, Refresh: {}, School: {}, Search: {}, User: {},
    DashboardBarChart: { name: 'DashboardBarChart' },
    api,
    structuredClone,
  })
  const { descriptor, render, bindings } = compilePage()
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import\s[\s\S]*?\sfrom\s+['"][^'"]+['"];?$/gm, '')
    + `\nglobalThis.model = {${bindings.join(',')}}`, context)
  return { model: context.model, calls, messages, render }
}

function simpleComponent(name, tag = 'div') {
  return Vue.defineComponent({
    name,
    setup(_props, { slots }) {
      return () => Vue.h(tag, {}, [
        slots.header?.(),
        slots.default?.({ row: {} }),
        slots.footer?.(),
      ])
    },
  })
}

async function htmlFor(model, render) {
  const app = Vue.createSSRApp({ setup: () => model, render })
  for (const name of [
    'ElCard', 'ElAlert', 'ElEmpty', 'ElButton', 'ElSelect', 'ElOption', 'ElIcon', 'ElRow', 'ElCol',
    'ElTag', 'ElInput', 'ElRadioGroup', 'ElRadioButton', 'ElTable', 'ElTableColumn', 'ElDatePicker',
    'ElPagination', 'DashboardBarChart',
  ]) app.component(name, simpleComponent(name))
  for (const name of ['Clock', 'DataAnalysis', 'Delete', 'Plus', 'Refresh', 'School', 'Search', 'User']) {
    app.component(name, simpleComponent(name))
  }
  app.directive('loading', {})
  return renderToString(app)
}

test('数据统计路由紧邻我的项目，且登录守卫继续生效', () => {
  assert.match(routerSource, /path: 'statistics', component: \(\) => import\('\.\/views\/DataStatistics\.vue'\)/)
  const projectsIndex = routerSource.indexOf("path: 'projects'")
  const statisticsIndex = routerSource.indexOf("path: 'statistics'")
  assert.ok(projectsIndex >= 0 && statisticsIndex > projectsIndex)
  assert.match(routerSource, /to\.path !== '\/login' && !localStorage\.getItem\('token'\)/)
})

test('顶部菜单将数据统计放在我的项目之后', () => {
  const projectsIndex = layoutSource.indexOf('index="/projects">我的项目')
  const statisticsIndex = layoutSource.indexOf('index="/statistics">数据统计')
  const configIndex = layoutSource.indexOf('index="/ai-config">AI 配置')
  assert.ok(projectsIndex >= 0)
  assert.ok(statisticsIndex > projectsIndex)
  assert.ok(configIndex > statisticsIndex)
})

test('图表组件按需加载 ECharts，并在更新和卸载时重建、释放实例', () => {
  assert.match(chartSource, /await import\('echarts'\)/)
  assert.match(chartSource, /disposeChart\(\)/)
  assert.match(chartSource, /new ResizeObserver/)
  assert.match(chartSource, /onBeforeUnmount/)
})

test('页面加载后选择首个内置数据文件并并行请求概览、学校表和分页明细', async () => {
  const setup = harness()
  await flush()
  const { model, calls } = setup
  assert.equal(model.selectedProjectId.value, 'builtin-student')
  assert.equal(model.selectedFileId.value, 'builtin:legacy.csv')
  assert.equal(model.overview.value.summary.total_submissions, 10)
  assert.deepEqual(model.overview.value.charts.city, [{ name: '济南市', value: 5 }, { name: '青岛市', value: 5 }])
  assert.equal(model.schoolRows.value[0].submit_count, 5)
  assert.equal(model.detailRows.value[0].username, 'u1')
  assert.deepEqual(calls.filter((call) => call.method === 'get').map((call) => call.url), [
    '/dashboard/sources',
    '/dashboard/overview',
  ])
  assert.deepEqual(calls.filter((call) => call.method === 'post').map((call) => call.url), [
    '/dashboard/school-table',
    '/dashboard/records',
  ])
})

test('内置旧版 CSV 会作为默认首选数据源并携带字符串标识', async () => {
  const setup = harness({ includeBuiltin: true })
  await flush()
  const { model, calls } = setup
  assert.equal(model.selectedProjectId.value, 'builtin-student')
  assert.equal(model.selectedFileId.value, 'builtin:legacy.csv')
  assert.equal(model.overview.value.file.file_name, 'legacy.csv')
  const overviewCall = calls.find((call) => call.method === 'get' && call.url === '/dashboard/overview')
  assert.equal(overviewCall.options.params.project_id, 'builtin-student')
  assert.equal(overviewCall.options.params.file_id, 'builtin:legacy.csv')
  const detailCall = calls.find((call) => call.method === 'post' && call.url === '/dashboard/records')
  assert.equal(detailCall.body.project_id, 'builtin-student')
  assert.equal(detailCall.body.file_id, 'builtin:legacy.csv')
})

test('单校查询、学校筛选和明细筛选使用独立接口参数', async () => {
  const setup = harness()
  await flush()
  const { model, calls } = setup

  model.schoolKeyword.value = '第一'
  model.schoolId.value = '1001'
  await model.loadOverview()
  const overviewCall = calls.filter((call) => call.method === 'get').at(-1)
  assert.equal(overviewCall.url, '/dashboard/overview')
  assert.equal(overviewCall.options.params.school_keyword, '第一')
  assert.equal(overviewCall.options.params.school_id, '1001')

  model.schoolFilters.city = '济南'
  model.schoolFilters.submitMin = '2'
  model.schoolFilters.submitMax = '5'
  await model.loadSchoolTable()
  const schoolCall = calls.filter((call) => call.method === 'post' && call.url === '/dashboard/school-table').at(-1)
  assert.deepEqual(JSON.parse(JSON.stringify(schoolCall.body.filters)), [
    { column: 'city', kind: 'text', value: '济南' },
    { column: 'submit_count', kind: 'range', minimum: 2, maximum: 5 },
  ])

  model.detailFilterRows.value = [{
    id: 1, column: 'update_time', kind: 'date', value: '', minimum: '', maximum: '',
    dateRange: ['2026-10-01', '2026-10-03'],
  }]
  await model.applyDetailFilters()
  const detailCall = calls.filter((call) => call.method === 'post' && call.url === '/dashboard/records').at(-1)
  assert.equal(detailCall.body.page, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(detailCall.body.filters)), [{
    column: 'update_time', kind: 'date', start: '2026-10-01', end: '2026-10-03',
  }])
})

test('明细分页、完整模式切换和非法数值筛选均有正确行为', async () => {
  const setup = harness()
  await flush()
  const { model, calls, messages } = setup

  model.detailPage.value = 2
  await model.loadRecords()
  let call = calls.filter((item) => item.method === 'post' && item.url === '/dashboard/records').at(-1)
  assert.equal(call.body.page, 2)
  assert.equal(call.body.mode, 'meta')

  model.detailMode.value = 'full'
  await model.handleModeChange()
  call = calls.filter((item) => item.method === 'post' && item.url === '/dashboard/records').at(-1)
  assert.equal(call.body.mode, 'full')
  assert.equal(model.detailMode.value, 'full')

  model.detailFilterRows.value = [{
    id: 2, column: 'submit_count', kind: 'range', value: '', minimum: 'abc', maximum: '', dateRange: [],
  }]
  const before = calls.length
  await model.applyDetailFilters()
  assert.equal(calls.length, before)
  assert.deepEqual(messages.at(-1), { type: 'warning', message: '筛选列「提交人次」的数值范围无效' })
})

test('无内置数据文件时渲染统一空状态', async () => {
  const setup = harness({ includeBuiltin: false })
  await flush()
  const { model, calls, render } = setup
  model.sourcesLoading.value = false
  const html = await htmlFor(model, render)
  assert.match(html, /暂无可统计数据文件/)
  assert.doesNotMatch(html, /前往我的项目上传/)
  assert.ok(calls.some((call) => call.method === 'get' && call.url === '/dashboard/sources'))
})

test('切换数据文件后清空旧查询状态并重新加载全部面板', async () => {
  const setup = harness()
  await flush()
  const { model, calls } = setup
  assert.ok(model.overview.value)
  model.schoolKeyword.value = '第一'
  model.detailMode.value = 'full'
  const before = calls.length
  model.selectedFileId.value = 'builtin:other.csv'
  model.resetForSelectedFile()
  assert.equal(model.overview.value, null)
  assert.equal(model.schoolKeyword.value, '')
  assert.equal(model.detailMode.value, 'meta')
  await flush()
  assert.ok(model.overview.value)
  const urls = calls.slice(before).map((call) => call.url)
  assert.ok(urls.includes('/dashboard/overview'))
  assert.ok(urls.includes('/dashboard/school-table'))
  assert.ok(urls.includes('/dashboard/records'))
})

test('数据文件选项只显示文件名并隐藏 _answers.csv 后缀', () => {
  const setup = harness()
  const { model } = setup
  assert.equal(
    model.fileOptionLabel({ file_name: '初中生心理体育问卷_answers.csv', size: 45214561 }),
    '初中生心理体育问卷',
  )
  assert.equal(
    model.fileOptionLabel({ file_name: 'survey-submissions-5.csv', size: 659400 }),
    'survey-submissions-5.csv',
  )
})






