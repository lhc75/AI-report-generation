<template>
  <section class="survey-workspace">
    <header class="page-heading">
      <div>
        <el-button v-if="id" text :icon="ArrowLeft" @click="router.push('/surveys')">项目列表</el-button>
        <h1>{{ project?.name || '批量问卷报告' }}</h1>
        <span v-if="project" class="muted">{{ project.year }} 年度 · {{ project.province }}</span>
      </div>
      <el-button v-if="!id" :icon="Plus" type="primary" @click="newProject = true">新建项目</el-button>
    </header>
    <el-table v-if="!id" :data="projects" @row-click="row => router.push('/surveys/' + row.id)">
      <el-table-column prop="name" label="项目名称" />
      <el-table-column prop="province" label="省份" width="160" />
      <el-table-column prop="year" label="年度" width="120" />
      <el-table-column width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="router.push('/surveys/' + row.id)">打开</el-button></template></el-table-column>
    </el-table>
    <el-dialog v-model="newProject" title="新建批量问卷项目" width="min(460px, 96vw)">
      <el-form label-width="80px" @submit.prevent="createProject">
        <el-form-item label="项目名称"><el-input v-model="form.name" maxlength="255" /></el-form-item>
        <el-form-item label="省份"><el-input v-model="form.province" maxlength="100" /></el-form-item>
        <el-form-item label="年度"><el-input-number v-model="form.year" :min="1900" :max="2200" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="newProject = false">取消</el-button><el-button type="primary" :loading="busy" :disabled="!form.name.trim() || !form.province.trim()" @click="createProject">创建</el-button></template>
    </el-dialog>

    <el-tabs v-if="project" v-model="tab">
      <el-tab-pane label="答卷导入" name="data">
        <div class="toolbar">
          <label class="upload"><el-icon><Upload /></el-icon> 上传答卷
            <input type="file" accept=".xlsx" multiple :disabled="busy" @change="uploadFiles($event, 'data')" />
          </label>
          <el-button type="primary" :loading="busy" :disabled="!sourceIds.length || importing" @click="startImport">导入所选答卷</el-button>
          <el-text v-if="catalog">当前版本 {{ project.dataset_id }} · {{ catalog.total.toLocaleString() }} 条答卷</el-text>
        </div>
        <el-alert v-if="project.latest_import?.error" :title="project.latest_import.error" type="error" :closable="false" />
        <el-alert v-else-if="importing" :title="'导入中 ' + (project.latest_import.progress.current || '等待调度')" type="info" :closable="false" />
        <el-collapse v-if="sources.length" v-model="openSources">
          <el-collapse-item v-for="asset in sources" :key="asset.id" :name="asset.id">
            <template #title>{{ asset.name }}</template>
            <el-checkbox :model-value="sourceIds.includes(asset.id)" @change="value => chooseSource(asset.id, value)">纳入本次导入</el-checkbox>
            <div v-for="sheet in mappings[asset.id] || []" :key="sheet.sheet" class="mapping">
              <h3>{{ sheet.sheet }}</h3>
              <div class="mapping-grid">
                <label>表头行号 <el-input-number v-model="sheet.header" :min="1" :max="200" /></label>
                <label v-for="[key, label] in mappingFields" :key="key">{{ label }}
                  <el-select v-model="sheet.fields[key]" clearable filterable placeholder="未映射">
                    <el-option v-for="(header, index) in sheet.headers" :key="index" :value="index" :label="(index + 1) + ' · ' + (header || '空表头')" />
                  </el-select>
                </label>
                <label v-if="sheet.fields.questionnaire == null">固定问卷名称 <el-input v-model="sheet.questionnaire" /></label>
              </div>
              <el-collapse>
                <el-collapse-item title="题型覆盖">
                  <div class="toolbar">
                    <el-select v-model="sheet.editColumn" filterable placeholder="选择题目" class="wide-select">
                      <el-option v-for="(header, index) in sheet.headers" :key="index" :value="index" :label="header || '第 ' + (index + 1) + ' 列'" />
                    </el-select>
                    <el-button :disabled="sheet.editColumn == null" @click="addQuestionRule(sheet)">添加覆盖</el-button>
                  </div>
                  <div v-for="(rule, column) in sheet.questions" :key="column" class="toolbar question-rule">
                    <span>{{ sheet.headers[column] || '第 ' + (+column + 1) + ' 列' }}</span>
                    <el-select v-model="rule.kind" class="medium-select"><el-option v-for="[value, label] in questionKinds" :key="value" :value="value" :label="label" /></el-select>
                    <el-input v-if="rule.kind === 'multiple'" v-model="rule.separator" placeholder="多选分隔符" class="medium-select" />
                    <el-button link type="danger" :icon="Delete" aria-label="删除覆盖" @click="delete sheet.questions[column]" />
                  </div>
                </el-collapse-item>
              </el-collapse>
            </div>
          </el-collapse-item>
        </el-collapse>
        <el-empty v-else description="暂无答卷" />
        <el-table v-if="catalog?.notices.length" :data="catalog.notices" class="spaced">
          <el-table-column prop="source" label="文件" min-width="200" /><el-table-column prop="issue" label="导入提示" min-width="240" /><el-table-column prop="total" label="数量" width="100" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="Word 模板" name="templates">
        <div class="toolbar">
          <el-select v-model="uploadLevel" class="medium-select" aria-label="模板层级"><el-option v-for="[value, label] in levels" :key="value" :value="value" :label="label" /></el-select>
          <label class="upload"><el-icon><Upload /></el-icon> 上传本级模板
            <input type="file" accept=".docx" multiple :disabled="busy" @change="uploadFiles($event, 'template')" />
          </label>
        </div>
        <el-table :data="templates">
          <el-table-column prop="name" label="模板文件" min-width="200" />
          <el-table-column label="层级" width="120"><template #default="{ row }">{{ levelNames[row.level] }}</template></el-table-column>
          <el-table-column label="状态" width="130"><template #default="{ row }">{{ row.ready ? '已确认' : '待确认' }}</template></el-table-column>
          <el-table-column width="145"><template #default="{ row }"><el-button link type="primary" @click="editTemplate(row)">预览与定位</el-button></template></el-table-column>
        </el-table>
        <el-dialog v-model="templateOpen" title="模板段落预览" width="min(730px, 96vw)">
          <template v-if="templateDraft">
            <el-form label-width="100px">
              <el-form-item label="标题替换位置"><el-select v-model="templateDraft.title_index" filterable><el-option v-for="p in templateDraft.paragraphs" :key="p.index" :label="p.text" :value="p.index" /></el-select></el-form-item>
              <el-form-item label="正文插入位置"><el-select v-model="templateDraft.body_index" filterable><el-option v-for="p in templateDraft.paragraphs" :key="p.index" :label="p.text" :value="p.index" /></el-select></el-form-item>
            </el-form>
            <ol class="template-preview">
              <li v-for="p in templateDraft.paragraphs" :key="p.index" :class="{ anchor: p.index === templateDraft.title_index || p.index === templateDraft.body_index }">{{ p.text }}</li>
            </ol>
          </template>
          <template #footer><el-button @click="templateOpen = false">取消</el-button><el-button type="primary" :loading="busy" :disabled="!templateDraft || templateDraft.title_index == null || templateDraft.body_index == null || templateDraft.title_index === templateDraft.body_index" @click="confirmTemplate">确认位置</el-button></template>
        </el-dialog>
      </el-tab-pane>

      <el-tab-pane label="年度指标" name="indicators">
        <el-empty v-if="!catalog" description="请先完成答卷导入" />
        <template v-else>
          <div class="toolbar">
            <label class="upload"><el-icon><Upload /></el-icon> 上传指标资料
              <input type="file" accept=".xlsx,.docx,.pdf" multiple :disabled="busy" @change="uploadFiles($event, 'indicator')" />
            </label>
            <el-select v-model="indicatorAsset" clearable placeholder="选择指标资料" class="wide-select"><el-option v-for="asset in indicatorAssets" :key="asset.id" :label="asset.name" :value="asset.id" /></el-select>
            <el-button :loading="busy" :disabled="!indicatorAsset" @click="suggestIndicators">AI 提取映射</el-button>
            <el-button :icon="Plus" @click="addIndicator">新增指标</el-button>
          </div>
          <div v-for="(rule, index) in indicatorDraft" :key="index" class="indicator-edit">
            <el-input v-model="rule.name" placeholder="指标名称" />
            <el-select v-model="rule.question_id" filterable placeholder="匹配问卷题目" @change="qid => changeQuestion(rule, qid)">
              <el-option v-for="q in questions" :key="q.id" :value="q.id" :label="q.questionnaire_name + ' · ' + q.title" />
            </el-select>
            <el-select v-model="rule.method" placeholder="统计方法">
              <el-option v-for="[value, label] in methods" :key="value" :value="value" :label="label" />
            </el-select>
            <el-select v-model="rule.options" multiple filterable collapse-tags placeholder="选项">
              <el-option v-for="value in optionChoices[rule.question_id] || rule.options" :key="value" :value="value" :label="value" />
            </el-select>
            <el-select v-model="rule.denominator" aria-label="分母"><el-option value="answered" label="本题有效作答" /><el-option value="sample" label="本问卷样本" /></el-select>
            <el-input v-model="rule.source" placeholder="来源位置" />
            <el-button link type="danger" :icon="Delete" aria-label="删除指标" @click="indicatorDraft.splice(index, 1)" />
          </div>
          <div v-if="indicatorDraft.length" class="toolbar spaced"><el-button type="primary" :loading="busy" @click="saveIndicators">确认指标版本</el-button></div>
          <el-table :data="indicators" class="spaced">
            <el-table-column prop="id" label="版本" width="100" /><el-table-column label="指标数"><template #default="{ row }">{{ row.rules.length }}</template></el-table-column>
            <el-table-column width="110"><template #default="{ row }"><el-button link type="primary" @click="editIndicators(row)">查看映射</el-button></template></el-table-column>
          </el-table>
        </template>
      </el-tab-pane>

      <el-tab-pane label="出报配置" name="plan">
        <el-empty v-if="!catalog" description="请先完成答卷导入" />
        <template v-else>
          <div class="toolbar">
            <el-radio-group :model-value="selection.mode" @change="chooseMode">
              <el-radio-button value="province">仅全省</el-radio-button>
              <el-radio-button value="levels">按层级全选</el-radio-button>
              <el-radio-button value="custom">自选组织</el-radio-button>
              <el-radio-button value="all">全选四级</el-radio-button>
            </el-radio-group>
            <el-text>{{ selectedCount.toLocaleString() }} 个组织</el-text>
          </div>
          <el-checkbox-group v-if="selection.mode === 'levels'" v-model="selection.enabled_levels" class="toolbar" aria-label="生成层级">
            <el-checkbox v-for="[key, label] in levels" :key="key" :value="key">{{ label }}</el-checkbox>
          </el-checkbox-group>
          <section v-if="selection.mode === 'custom' || selection.mode === 'all'" class="section-block">
            <div class="toolbar">
              <el-input v-model="orgSearch" :prefix-icon="Search" clearable placeholder="搜索组织名称或学校 ID" class="wide-select" />
              <el-select v-model="orgLevel" clearable placeholder="所有层级" class="medium-select"><el-option v-for="[value, label] in levels" :key="value" :value="value" :label="label" /></el-select>
              <el-select v-model="jurisdiction" clearable filterable placeholder="所有辖区" class="wide-select"><el-option v-for="row in jurisdictions" :key="row.id" :label="row.path" :value="row.id" /></el-select>
              <el-button :disabled="!visibleOrganizations.length" @click="chooseRows(visibleOrganizations, true)">全选筛选结果</el-button>
              <el-button :disabled="!visibleOrganizations.length" @click="chooseRows(visibleOrganizations, false)">取消筛选结果</el-button>
            </div>
            <div v-if="jurisdiction" class="toolbar">
              <el-button size="small" @click="chooseRows(descendants('county'), true)">全选辖内区县</el-button>
              <el-button size="small" @click="chooseRows(descendants('school'), true)">全选辖内学校</el-button>
            </div>
            <el-table :data="organizationPage" height="319">
              <el-table-column width="56"><template #default="{ row }"><el-checkbox :model-value="isSelected(row.id)" :aria-label="'选择 ' + row.name" @change="value => chooseRows([row], value)" /></template></el-table-column>
              <el-table-column label="组织" min-width="300"><template #default="{ row }"><span :style="{ paddingLeft: levels.findIndex(([key]) => key === row.level) * 9 + 'px' }">{{ row.path }}</span></template></el-table-column>
              <el-table-column label="层级" width="85"><template #default="{ row }">{{ levelNames[row.level] }}</template></el-table-column>
              <el-table-column prop="school_code" label="学校 ID" width="115" />
              <el-table-column label="辖区" width="90"><template #default="{ row }"><el-button v-if="row.level !== 'school'" type="primary" link @click="jurisdiction = row.id">查看</el-button></template></el-table-column>
            </el-table>
            <el-pagination v-model:current-page="orgPage" :page-size="30" :total="visibleOrganizations.length" layout="prev, pager, next, total" />
          </section>
          <div class="level-config">
            <strong>层级</strong><strong>问卷</strong><strong>候选模板</strong><strong>输出方式</strong>
            <template v-for="[key, label] in configuredLevels" :key="key">
              <span>{{ label }}</span>
              <el-select v-model="selection.levels[key].questionnaires" multiple filterable collapse-tags placeholder="全部问卷">
                <el-option v-for="row in catalog.questionnaires" :key="row.id" :value="row.id" :label="row.name" />
              </el-select>
              <el-select v-model="selection.levels[key].templates" multiple filterable collapse-tags placeholder="本级默认模板" @change="cleanOverrides(key)">
                <el-option v-for="row in levelTemplates(key)" :key="row.id" :value="row.id" :label="row.name + ' #' + row.id" />
              </el-select>
              <el-checkbox v-if="key !== 'province'" v-model="selection.levels[key].split">按问卷分开</el-checkbox>
              <span v-else>合并一份</span>
            </template>
          </div>
          <div class="toolbar spaced">
            <label class="upload"><el-icon><Upload /></el-icon> 上传年度指标
              <input type="file" accept=".xlsx,.docx,.pdf" multiple :disabled="busy" @change="uploadFiles($event, 'indicator')" />
            </label>
            <el-select v-model="selection.indicator_assets" multiple filterable collapse-tags placeholder="选择年度指标资料"
                       aria-label="年度指标资料" :disabled="!!selection.indicator_id" class="wide-select">
              <el-option v-for="asset in indicatorAssets" :key="asset.id" :label="asset.name" :value="asset.id" />
            </el-select>
            <el-select v-if="indicators.length" v-model="selection.indicator_id" clearable
                       :disabled="!!selection.indicator_assets.length" placeholder="已确认的指标版本" class="wide-select">
              <el-option v-for="row in indicators" :key="row.id" :value="row.id" :label="'指标版本 #' + row.id + ' · ' + row.rules.length + ' 个指标'" />
            </el-select>
            <el-button type="primary" :disabled="!selectedCount" :loading="busy" @click="makePreview">预览出报清单</el-button>
          </div>
          <section v-if="preview" class="section-block">
            <div class="toolbar"><h2>预计生成 {{ preview.reports.toLocaleString() }} 份正式 Word</h2></div>
            <el-table :data="previewLevels">
              <el-table-column prop="label" label="层级" /><el-table-column prop="organizations" label="组织数" /><el-table-column prop="responses" label="答卷数" /><el-table-column prop="reports" label="Word 份数" /><el-table-column prop="skipped" label="跳过组织数" />
            </el-table>
            <div class="toolbar spaced"><el-input v-model="previewSearch" clearable :prefix-icon="Search" placeholder="搜索清单中的组织" class="wide-select" /></div>
            <el-table :data="previewPageRows" height="330">
              <el-table-column prop="path" label="组织" min-width="240" />
              <el-table-column label="问卷及答卷数" min-width="245"><template #default="{ row }"><div v-for="(count, key) in row.samples" :key="key">{{ preview.questionnaires[key] }} · {{ count.toLocaleString() }}<span v-if="!count">（无样本）</span></div></template></el-table-column>
              <el-table-column label="Word 模板" min-width="190"><template #default="{ row }">
                <el-select v-if="selection.levels[row.level].templates.length" :model-value="selection.overrides[row.id] ?? row.template_id"
                           @change="value => changeTemplateAssignment(row.id, value)">
                  <el-option v-for="key in selection.levels[row.level].templates" :key="key" :value="key" :label="templateNames[key] + ' #' + key" />
                </el-select>
                <span v-else>本级默认模板</span>
              </template></el-table-column>
              <el-table-column label="生成份数" width="100"><template #default="{ row }">{{ row.reports || '无答卷' }}</template></el-table-column>
            </el-table>
            <div class="toolbar spaced between">
              <el-pagination v-model:current-page="previewPage" :page-size="30" :total="filteredPreview.length" layout="prev, pager, next, total" />
              <el-button type="primary" :loading="busy" :disabled="!preview.reports" @click="submitBatch">一键生成</el-button>
            </div>
          </section>
        </template>
      </el-tab-pane>

      <el-tab-pane label="批次与下载" name="batches">
        <div class="toolbar">
          <el-select v-model="batchId" placeholder="选择批次" class="wide-select">
            <el-option v-for="row in batches" :key="row.id" :value="row.id" :label="'批次 #' + row.id + ' · ' + row.counts.done + '/' + row.total + ' 已完成'" />
          </el-select>
          <el-button :icon="Refresh" @click="refreshBatches">刷新</el-button>
        </div>
        <el-empty v-if="!batch" description="暂无出报批次" />
        <template v-else>
          <div v-if="batch.generation && batch.generation.state !== 'done'" class="toolbar" aria-live="polite">
            <el-alert :type="batch.generation.state === 'failed' ? 'error' : 'info'" :closable="false"
                      :title="batch.generation.error || batch.generation.stage" />
            <el-button v-if="batch.generation.state === 'failed'" @click="retryBatch">继续处理</el-button>
          </div>
          <el-collapse v-if="batch.generation?.missing.length">
            <el-collapse-item :title="'年度指标缺项 · ' + batch.generation.missing.length + ' 项'">
              <el-table :data="batch.generation.missing">
                <el-table-column label="指标" prop="name" />
                <el-table-column label="资料位置" prop="source" />
                <el-table-column label="原因" prop="reason" />
              </el-table>
            </el-collapse-item>
          </el-collapse>
          <el-progress :percentage="Math.floor(100 * (batch.counts.done + batch.counts.failed) / batch.total)" />
          <div class="toolbar spaced">
            <el-text>已完成 {{ batch.counts.done }} · 运行中 {{ batch.counts.running }} · 排队 {{ batch.counts.queued }} · 失败 {{ batch.counts.failed }}</el-text>
          </div>
          <div class="toolbar">
            <el-select v-model="zipLevel" clearable placeholder="ZIP 全部层级" class="medium-select"><el-option v-for="[value, label] in levels" :key="value" :value="value" :label="label" /></el-select>
            <el-select v-model="zipOrg" clearable filterable placeholder="ZIP 全辖区" class="wide-select"><el-option v-for="org in batchJurisdictions" :key="org.id" :value="org.id" :label="org.path" /></el-select>
            <el-input-number v-model="zipPart" :min="1" :max="zipParts" aria-label="ZIP 分包序号" />
            <el-button :icon="Download" :disabled="!batch.counts.done" :loading="downloading" @click="downloadZip(false)">下载报告 ZIP</el-button>
            <el-button v-if="batch.generation" :icon="Download" :disabled="!batch.counts.done" :loading="downloading"
                       @click="downloadZip(true)">下载红字 ZIP</el-button>
            <el-text>每包最多 100 份</el-text>
          </div>
          <div class="toolbar"><el-select v-model="reportState" clearable placeholder="全部状态" class="medium-select"><el-option v-for="[key, label] in Object.entries(states)" :key="key" :value="key" :label="label" /></el-select></div>
          <el-table :data="batch.reports">
            <el-table-column prop="org_path" label="组织" min-width="260" />
            <el-table-column label="问卷" min-width="200"><template #default="{ row }">{{ row.questionnaires.join('、') }}</template></el-table-column>
            <el-table-column label="状态" width="100"><template #default="{ row }">{{ states[row.state] }}</template></el-table-column>
            <el-table-column label="处理说明" min-width="180"><template #default="{ row }">
              {{ row.error || row.notice }}
              <el-popover v-if="row.missing?.length" trigger="click" width="min(440px, 90vw)">
                <template #reference><el-button link type="primary">查看缺项</el-button></template>
                <ul class="missing-items">
                  <li v-for="(item, index) in row.missing" :key="index">
                    <strong>{{ item.name }}</strong> · {{ item.reason }}
                    <div v-if="item.source" class="muted">{{ item.source }}</div>
                  </li>
                </ul>
              </el-popover>
            </template></el-table-column>
            <el-table-column width="165"><template #default="{ row }">
              <el-button v-if="row.state === 'done'" link type="primary" :icon="Download" @click="downloadWord(row)">Word</el-button>
              <el-button v-if="row.analysis" link type="primary" @click="downloadAnalysis(row)">红字解析</el-button>
              <el-button v-if="row.state === 'failed'" link type="primary" @click="retry(row)">重试</el-button>
            </template></el-table-column>
          </el-table>
          <el-pagination v-model:current-page="reportPage" :page-size="50" :total="reportState ? batch.counts[reportState] : batch.total" layout="prev, pager, next, total" />
        </template>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<script setup>
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Delete, Download, Plus, Refresh, Search, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import { inJurisdiction, levels, toggleOrganizations } from '../surveySelection'

const route = useRoute()
const router = useRouter()
const id = computed(() => Number(route.params.id) || null)
const endpoint = computed(() => '/surveys' + (id.value ? '/' + id.value : ''))
const levelNames = Object.fromEntries(levels)
const states = { queued: '排队', running: '运行中', done: '已完成', failed: '失败' }
const mappingFields = [['city', '市州'], ['county', '区县'], ['school', '学校名称'], ['school_id', '学校 ID'], ['questionnaire', '问卷类型']]
const methods = [['count', '计数'], ['rate', '比例'], ['multiple_rate', '多选勾选率'], ['mean', '均值']]
const questionKinds = [['auto', '自动'], ['single', '单选'], ['multiple', '多选'], ['number', '数值'], ['ignore', '忽略']]
const clone = value => JSON.parse(JSON.stringify(value))
const projects = ref([])
const project = ref(null)
const catalog = ref(null)
const tab = ref('data')
const busy = ref(false)
const downloading = ref(false)
const newProject = ref(false)
const form = reactive({ name: '', province: '', year: new Date().getFullYear() })
const sourceIds = ref([])
const mappings = reactive({})
const openSources = ref([])
const sources = computed(() => project.value?.assets.filter(asset => asset.kind === 'data') || [])
const templates = computed(() => project.value?.assets.filter(asset => asset.kind === 'template') || [])
const indicatorAssets = computed(() => project.value?.assets.filter(asset => asset.kind === 'indicator') || [])
const importing = computed(() => ['queued', 'running'].includes(project.value?.latest_import?.state))
const uploadLevel = ref('province')
const templateOpen = ref(false)
const templateDraft = ref(null)
const indicatorAsset = ref(null)
const questions = ref([])
const optionChoices = reactive({})
const indicatorDraft = ref([])
const indicators = computed(() => project.value?.indicators.filter(row => row.dataset_id === project.value.dataset_id) || [])
const templateNames = computed(() => Object.fromEntries(templates.value.map(row => [row.id, row.name])))
const levelTemplates = level => templates.value.filter(row => row.ready && row.level === level)

const selection = reactive(emptySelection())
const orgSearch = ref('')
const orgLevel = ref('')
const jurisdiction = ref(null)
const orgPage = ref(1)
const preview = ref(null)
const previewSearch = ref('')
const previewPage = ref(1)
const previewedKey = ref('')
const requestId = ref('')
const orgs = computed(() => catalog.value?.organizations || [])
const orgById = computed(() => new Map(orgs.value.map(org => [org.id, org])))
const jurisdictions = computed(() => orgs.value.filter(org => org.level !== 'school'))
const included = computed(() => new Set(selection.org_ids))
const excluded = computed(() => new Set(selection.excluded))
const activeOrganizations = computed(() => orgs.value.filter(org => isSelected(org.id)))
const selectedCount = computed(() => activeOrganizations.value.length)
const configuredLevels = computed(() => {
  const keys = new Set(activeOrganizations.value.map(org => org.level))
  return levels.filter(([key]) => keys.has(key))
})
const visibleOrganizations = computed(() => {
  const keyword = orgSearch.value?.trim().toLowerCase() || ''
  return orgs.value.filter(org =>
    (!orgLevel.value || org.level === orgLevel.value) &&
    (!jurisdiction.value || inJurisdiction(org, jurisdiction.value, orgById.value)) &&
    (!keyword || org.path.toLowerCase().includes(keyword) || org.school_code?.toLowerCase().includes(keyword)))
})
const organizationPage = computed(() => visibleOrganizations.value.slice((orgPage.value - 1) * 30, orgPage.value * 30))
const previewLevels = computed(() => levels.map(([key, label]) => ({ label, ...preview.value?.levels[key] })))
const filteredPreview = computed(() => {
  const keyword = previewSearch.value?.trim().toLowerCase() || ''
  return preview.value?.organizations.filter(row => row.path.toLowerCase().includes(keyword) || row.school_code?.toLowerCase().includes(keyword)) || []
})
const previewPageRows = computed(() => filteredPreview.value.slice((previewPage.value - 1) * 30, previewPage.value * 30))

const batches = ref([])
const batchId = ref(null)
const batch = ref(null)
const batchJurisdictions = ref([])
const reportState = ref('')
const reportPage = ref(1)
const zipLevel = ref('')
const zipOrg = ref(null)
const zipPart = ref(1)
const zipParts = computed(() => {
  const count = zipLevel.value ? batch.value?.levels[zipLevel.value]?.reports : batch.value?.total
  return Math.max(1, Math.ceil((count || 0) / 100))
})

function emptySelection() {
  return {
    mode: 'province', enabled_levels: ['city'], org_ids: [], excluded: [], overrides: {},
    indicator_id: null, indicator_assets: [],
    levels: Object.fromEntries(levels.map(([key]) => [key, { questionnaires: [], templates: [], split: false }])),
  }
}

async function withBusy(operation) {
  if (busy.value) return
  busy.value = true
  try { await operation() } catch {
    // The shared API displays request errors.
  } finally { busy.value = false }
}

async function createProject() {
  await withBusy(async () => {
    const { data } = await api.post('/surveys', form)
    newProject.value = false
    await router.push('/surveys/' + data.id)
  })
}

function prepareSource(asset, selectNew) {
  if (mappings[asset.id]) return
  const previous = project.value?.latest_import?.sources.find(source => source.asset_id === asset.id)
  mappings[asset.id] = asset.sheets.map(sheet => {
    const saved = previous?.sheets.find(row => row.sheet === sheet.sheet)
    return { ...clone(sheet), questions: {}, ...(saved ? clone(saved) : {}) }
  })
  if (selectNew || previous || !project.value?.latest_import) chooseSource(asset.id, true)
}

function chooseSource(assetId, value) {
  sourceIds.value = toggleOrganizations(sourceIds.value, [{ id: assetId }], value)
}

function addQuestionRule(sheet) {
  sheet.questions[sheet.editColumn] = { kind: 'auto', separator: '；' }
  sheet.editColumn = null
}

async function uploadFiles(event, kind) {
  const files = [...event.target.files]
  event.target.value = ''
  if (!files.length) return
  await withBusy(async () => {
    for (const file of files) {
      const payload = new FormData()
      payload.append('file', file)
      payload.append('kind', kind)
      if (kind === 'template') payload.append('level', uploadLevel.value)
      const { data } = await api.post(endpoint.value + '/assets', payload, { timeout: 0 })
      if (kind === 'data') {
        prepareSource(data, true)
        chooseSource(data.id, true)
      }
      if (kind === 'indicator') {
        indicatorAsset.value = data.id
        selection.indicator_id = null
        selection.indicator_assets = toggleOrganizations(selection.indicator_assets, [data], true)
      }
      if (kind === 'template' && files.length === 1) editTemplate(data)
    }
    await loadProject()
  })
}

async function startImport() {
  await withBusy(async () => {
    const sources = sourceIds.value.map(asset_id => ({
      asset_id,
      sheets: mappings[asset_id].map(({ sheet, header, fields, questionnaire, questions }) => ({
        sheet, header, questionnaire, questions,
        fields: Object.fromEntries(Object.entries(fields).filter(([, value]) => Number.isInteger(value))),
      })),
    }))
    await api.post(endpoint.value + '/imports', { sources })
    await loadProject()
  })
}

function editTemplate(row) {
  templateDraft.value = clone(row)
  templateOpen.value = true
}

async function confirmTemplate() {
  await withBusy(async () => {
    const { id, level, title_index, body_index } = templateDraft.value
    const { data } = await api.put(endpoint.value + '/templates/' + id, { title_index, body_index })
    if (!selection.levels[level].templates.length) selection.levels[level].templates = [data.id]
    templateOpen.value = false
    await loadProject()
  })
}

function addIndicator() {
  indicatorDraft.value.push({ name: '', question_id: null, method: 'count', options: [], denominator: 'answered', source: '' })
}

async function loadOptions(questionId) {
  if (!questionId || optionChoices[questionId]) return
  const { data } = await api.get(endpoint.value + '/questions', { params: { dataset_id: project.value.dataset_id, question_id: questionId } })
  optionChoices[questionId] = data
}

function changeQuestion(rule, questionId) {
  rule.options = []
  void loadOptions(questionId).catch(() => {})
}

function editIndicators(version) {
  indicatorDraft.value = clone(version.rules)
  indicatorAsset.value = version.asset_id
  for (const rule of version.rules) void loadOptions(rule.question_id).catch(() => {})
}

async function suggestIndicators() {
  await withBusy(async () => {
    const { data } = await api.post(endpoint.value + '/indicators/suggest', null, {
      params: { dataset_id: project.value.dataset_id, asset_id: indicatorAsset.value }, timeout: 180000,
    })
    editIndicators({ rules: data.map(rule => ({
      name: String(rule.name || ''), question_id: Number(rule.question_id) || null,
      method: methods.some(([key]) => key === rule.method) ? rule.method : 'count',
      options: Array.isArray(rule.options) ? rule.options.filter(value => typeof value === 'string') : [],
      denominator: rule.denominator === 'sample' ? 'sample' : 'answered', source: String(rule.source || ''),
    })), asset_id: indicatorAsset.value })
  })
}

async function saveIndicators() {
  await withBusy(async () => {
    const { data } = await api.post(endpoint.value + '/indicators', {
      dataset_id: project.value.dataset_id, asset_id: indicatorAsset.value || null, rules: indicatorDraft.value,
    })
    selection.indicator_id = data.id
    await loadProject()
    ElMessage.success('指标版本已确认')
  })
}

function chooseMode(mode) {
  selection.mode = mode
  selection.excluded = []
  preview.value = null
}

function isSelected(orgId) {
  if (selection.mode === 'province') return orgId === 1
  if (selection.mode === 'levels') return selection.enabled_levels.includes(orgById.value.get(orgId)?.level)
  return selection.mode === 'all' ? !excluded.value.has(orgId) : included.value.has(orgId)
}

function chooseRows(rows, value) {
  if (selection.mode === 'all') selection.excluded = toggleOrganizations(selection.excluded, rows, !value)
  else selection.org_ids = toggleOrganizations(selection.org_ids, rows, value)
}

function descendants(level) {
  return orgs.value.filter(org => org.level === level && inJurisdiction(org, jurisdiction.value, orgById.value))
}

function cleanOverrides(level) {
  for (const [orgId, templateId] of Object.entries(selection.overrides)) {
    if (orgById.value.get(Number(orgId))?.level === level && !selection.levels[level].templates.includes(templateId)) {
      delete selection.overrides[orgId]
    }
  }
}

function payload() {
  return { ...clone(selection), dataset_id: project.value?.dataset_id, automatic: true, indicator_id: selection.indicator_id || null }
}

const baseKey = computed(() => {
  const { overrides, ...base } = payload()
  return JSON.stringify(base)
})
watch(baseKey, () => { preview.value = null })
watch([orgSearch, orgLevel, jurisdiction], () => { orgPage.value = 1 })
watch(previewSearch, () => { previewPage.value = 1 })
watch([zipLevel, zipOrg], () => { zipPart.value = 1 })

async function makePreview() {
  await withBusy(async () => {
    const key = baseKey.value
    const { data } = await api.post(endpoint.value + '/preview', payload())
    if (baseKey.value === key) {
      preview.value = data
      previewedKey.value = key
      requestId.value = crypto.randomUUID()
      previewPage.value = 1
    }
  })
}

function changeTemplateAssignment(orgId, value) {
  selection.overrides[orgId] = value
}

async function submitBatch() {
  if (previewedKey.value !== baseKey.value) return
  await withBusy(async () => {
    const { data } = await api.post(endpoint.value + '/generate', { ...payload(), request_id: requestId.value })
    batchId.value = data.id
    tab.value = 'batches'
    preview.value = null
    await refreshBatches()
  })
}

async function loadProject() {
  const current = id.value
  if (!current) return
  const { data } = await api.get('/surveys/' + current)
  if (id.value !== current) return
  const previous = project.value?.dataset_id
  const wasImporting = importing.value
  project.value = data
  for (const asset of sources.value) prepareSource(asset, false)
  if (data.dataset_id && data.dataset_id !== previous) {
    Object.assign(selection, emptySelection())
    selection.indicator_assets = indicatorAssets.value.map(asset => asset.id)
    catalog.value = null
    questions.value = []
    indicatorDraft.value = []
    for (const key of Object.keys(optionChoices)) delete optionChoices[key]
    const responses = await Promise.all(['catalog', 'questions'].map(path =>
      api.get('/surveys/' + current + '/' + path, { params: { dataset_id: data.dataset_id } })))
    if (id.value === current && project.value?.dataset_id === data.dataset_id) {
      catalog.value = responses[0].data
      questions.value = responses[1].data
      if (wasImporting && tab.value === 'data') tab.value = 'plan'
    }
  }
}

async function loadBatch() {
  const activeBatch = batchId.value
  const activeProject = id.value
  if (!activeBatch || !activeProject) return
  const state = reportState.value
  const offset = (reportPage.value - 1) * 50
  const { data } = await api.get('/surveys/' + activeProject + '/batches/' + activeBatch, {
    params: { state: state || undefined, offset },
  })
  if (id.value !== activeProject || batchId.value !== activeBatch || reportState.value !== state || offset !== (reportPage.value - 1) * 50) return
  const oldDataset = batch.value?.dataset_id
  batch.value = data
  if (oldDataset !== data.dataset_id || !batchJurisdictions.value.length) {
    if (data.dataset_id === project.value?.dataset_id && catalog.value) batchJurisdictions.value = jurisdictions.value
    else {
      const result = await api.get(endpoint.value + '/catalog', { params: { dataset_id: data.dataset_id } })
      if (id.value === activeProject && batchId.value === activeBatch) {
        batchJurisdictions.value = result.data.organizations.filter(row => row.level !== 'school')
      }
    }
  }
}

async function refreshBatches() {
  const current = id.value
  if (!current) return
  const { data } = await api.get('/surveys/' + current + '/batches')
  if (id.value !== current) return
  batches.value = data
  if (!batchId.value) batchId.value = data[0]?.id || null
  else await loadBatch()
}

async function retry(row) {
  await withBusy(async () => {
    await api.post(endpoint.value + '/batches/' + batchId.value + '/reports/' + row.id + '/retry')
    await loadBatch()
  })
}

async function retryBatch() {
  await withBusy(async () => {
    await api.post(endpoint.value + '/batches/' + batchId.value + '/retry')
    await loadBatch()
  })
}

async function getDownload(path, params, fallback) {
  if (downloading.value) return
  downloading.value = true
  try {
    const { data, headers } = await api.get(endpoint.value + '/batches/' + batchId.value + path, {
      params, responseType: 'blob', timeout: 0, silentError: true,
    })
    const filename = headers['content-disposition']?.match(/filename\*=utf-8''([^;]+)/i)?.[1]
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    link.href = url
    link.download = filename ? decodeURIComponent(filename) : fallback
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) {
    let message = '下载失败'
    try { message = JSON.parse(await error.response.data.text()).detail || message } catch { /* use default */ }
    ElMessage.error(message)
  } finally { downloading.value = false }
}

function downloadWord(row) {
  return getDownload('/reports/' + row.id + '/download', {}, '问卷报告.docx')
}

function downloadAnalysis(row) {
  return getDownload('/reports/' + row.id + '/analysis', {}, '红字解析.docx')
}

function downloadZip(analysis) {
  return getDownload('/zip', { level: zipLevel.value || undefined, org_id: zipOrg.value || undefined,
    part: zipPart.value, analysis }, analysis ? '红字解析.zip' : '问卷报告.zip')
}

watch(id, async active => {
  project.value = null
  catalog.value = null
  batch.value = null
  batches.value = []
  batchId.value = null
  batchJurisdictions.value = []
  Object.assign(selection, emptySelection())
  indicatorAsset.value = null
  indicatorDraft.value = []
  orgSearch.value = ''
  orgLevel.value = ''
  jurisdiction.value = null
  reportState.value = ''
  sourceIds.value = []
  openSources.value = []
  preview.value = null
  tab.value = 'data'
  for (const key of Object.keys(mappings)) delete mappings[key]
  try {
    if (active) {
      await loadProject()
      await refreshBatches()
      const unfinished = batches.value.find(row => row.generation?.state !== 'failed' && (row.counts.queued || row.counts.running))
      if (unfinished) {
        batchId.value = unfinished.id
        tab.value = 'batches'
      } else if (catalog.value) tab.value = 'plan'
    }
    else projects.value = (await api.get('/surveys')).data
  } catch { /* error handled by API interceptor */ }
}, { immediate: true })
watch(batchId, () => {
  reportPage.value = 1
  zipOrg.value = null
  zipPart.value = 1
  if (batchId.value) void loadBatch().catch(() => {})
})
watch(reportState, () => { reportPage.value = 1; void loadBatch().catch(() => {}) })
watch(reportPage, () => { void loadBatch().catch(() => {}) })

let polling = false
const timer = setInterval(async () => {
  if (!id.value || polling || busy.value) return
  polling = true
  try {
    if (importing.value) await loadProject()
    if (tab.value === 'batches' && batch.value?.generation?.state !== 'failed'
        && (batch.value?.counts.queued || batch.value?.counts.running)) await refreshBatches()
  } catch { /* error handled by API interceptor */ }
  finally { polling = false }
}, 2500)
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.survey-workspace { max-width: 1500px; margin: 0 auto; min-width: 0; }
.page-heading { display: flex; justify-content: space-between; align-items: center; gap: 14px; margin-bottom: 15px; }
h1 { font-size: 24px; margin: 4px 0 7px; overflow-wrap: anywhere; }
h2 { font-size: 17px; margin: 0; }
h3 { font-size: 14px; margin: 7px 0; overflow-wrap: anywhere; }
.muted { color: var(--el-text-color-secondary); font-size: 14px; }
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.toolbar :deep(.el-button + .el-button) { margin-left: 0; }
.spaced { margin-top: 14px; }
.between { justify-content: space-between; }
.section-block, .mapping { padding-block: 12px; border-block-start: 1px solid var(--el-border-color-light); margin-top: 12px; }
.upload { display: inline-flex; align-items: center; justify-content: center; gap: 6px; position: relative; overflow: hidden;
  min-height: 32px; padding: 0 14px; background: #fff; border: 1px solid var(--el-border-color); border-radius: 4px;
  font-size: 14px; cursor: pointer; box-sizing: border-box; }
.upload input { opacity: 0; position: absolute; inset: 0; width: 100%; cursor: pointer; }
.upload:focus-within { outline: 2px solid var(--el-color-primary); }
.wide-select { width: min(100%, 280px); }
.medium-select { width: min(100%, 150px); }
.mapping-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(185px, 1fr)); gap: 12px; margin-bottom: 12px; }
.mapping-grid label { display: grid; gap: 4px; }
.mapping-grid .el-select, .mapping-grid .el-input, .mapping-grid .el-input-number { width: 100%; }
.level-config { display: grid; grid-template-columns: 64px minmax(120px, 1fr) minmax(120px, 1fr) 125px; gap: 10px; align-items: center; }
.indicator-edit { display: grid; grid-template-columns: minmax(105px, 1fr) minmax(155px, 2fr) repeat(4, minmax(105px, 1fr)) auto; gap: 8px; margin-bottom: 10px; }
.template-preview { max-height: 310px; overflow: auto; padding-inline-start: 27px; }
.template-preview li { padding: 6px 8px; overflow-wrap: anywhere; }
.missing-items { padding-inline-start: 20px; margin: 0; max-height: 280px; overflow: auto; overflow-wrap: anywhere; }
.missing-items li { padding-block: 5px; }
.anchor { background: var(--el-color-primary-light-9); color: var(--el-color-primary-dark-2); }
.question-rule span { min-width: min(100%, 240px); max-width: 100%; overflow-wrap: anywhere; }
.el-pagination { margin-top: 10px; max-width: 100%; overflow: auto; }
@media (max-width: 780px) {
  .level-config { grid-template-columns: 55px minmax(90px, 1fr) minmax(90px, 1fr) auto; gap: 6px; font-size: 12px; }
  .indicator-edit { grid-template-columns: repeat(2, minmax(105px, 1fr)); }
}
</style>
