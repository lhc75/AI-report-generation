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
      <el-table-column label="操作" width="150"><template #default="{ row }">
        <el-button link type="primary" @click.stop="router.push('/surveys/' + row.id)">打开</el-button>
        <el-button link type="danger" :disabled="busy" @click.stop="removeProject(row)">删除</el-button>
      </template></el-table-column>
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
      <el-tab-pane label="问卷与答卷" name="data">
        <section class="toolbar school-toolbar">
          <h2>学校信息</h2>
          <el-button :icon="Download" :disabled="downloading" @click="downloadSchoolTemplate">下载 Excel 模板</el-button>
          <label class="upload"><el-icon><Upload /></el-icon> {{ schoolRoster ? '替换学校信息' : '上传学校信息' }}
            <input :aria-label="schoolRoster ? '替换学校信息' : '上传学校信息'" type="file" accept=".xlsx"
                   :disabled="busy" @change="uploadFiles($event, 'school')" />
          </label>
          <template v-if="schoolRoster">
            <el-text truncated class="school-file" :title="schoolRoster.name">{{ schoolRoster.name }}</el-text>
            <el-text>小学 {{ schoolRoster.counts.primary.toLocaleString() }} · 初中 {{ schoolRoster.counts.middle.toLocaleString() }}</el-text>
            <el-button link type="danger" :disabled="busy" @click="removeAsset(schoolRoster)">删除</el-button>
          </template>
        </section>
        <div class="upload-grid">
          <section class="file-panel">
            <div class="panel-heading"><h2>问卷原稿</h2><span class="muted">DOCX / PDF</span></div>
            <label class="upload"><el-icon><Upload /></el-icon> 上传问卷原稿
              <input aria-label="上传问卷原稿" type="file" accept=".docx,.pdf" multiple :disabled="busy"
                     @change="uploadFiles($event, 'questionnaire')" />
            </label>
            <div v-for="asset in originals" :key="asset.id" class="survey-file">
              <div class="file-info">
                <span class="file-label" :title="asset.name">{{ asset.name }}</span>
                <el-text v-if="!asset.has_text" type="warning" size="small">未提取到文字</el-text>
                <el-text v-else-if="!catalog" type="info" size="small">已上传 · 待导入答卷</el-text>
                <el-text v-else-if="!asset.linked_questionnaire_id" type="warning" size="small">待关联</el-text>
                <el-text v-else type="success" size="small">{{ asset.questionnaire_name ? '已关联' : '自动关联' }}</el-text>
              </div>
              <el-select v-if="catalog" :model-value="asset.linked_questionnaire_id ?? undefined" clearable filterable
                         :aria-label="'关联问卷 · ' + asset.name" placeholder="选择问卷类型" class="original-select"
                         :disabled="busy || importing" @change="value => linkOriginal(asset.id, value)">
                <el-option v-for="row in catalog.questionnaires" :key="row.id" :value="row.id" :label="row.name" />
              </el-select>
              <el-button link type="danger" :disabled="busy" :aria-label="'删除 ' + asset.name" @click="removeAsset(asset)">删除</el-button>
            </div>
            <div v-for="entry in uploadFeedback.questionnaire" :key="entry.key" class="survey-file">
              <span class="file-label" :title="entry.name">{{ entry.name }}</span>
              <el-text :type="entry.error ? 'danger' : 'info'" size="small">{{ entry.error || '上传中' }}</el-text>
            </div>
            <el-text v-if="!originals.length && !uploadFeedback.questionnaire.length" class="file-empty">暂无问卷原稿</el-text>
          </section>
          <section class="file-panel">
            <div class="panel-heading"><h2>答卷数据</h2><span class="muted">Excel / CSV</span></div>
            <label class="upload"><el-icon><Upload /></el-icon> 上传答卷
              <input aria-label="上传答卷" type="file" accept=".xlsx,.xls,.csv" multiple :disabled="busy || importing"
                     @change="uploadFiles($event, 'data')" />
            </label>
            <div v-for="asset in sources" :key="asset.id" class="survey-file">
              <el-checkbox :model-value="sourceIds.includes(asset.id)" :disabled="busy || importing"
                           :aria-label="'纳入导入 · ' + asset.name" @change="value => chooseSource(asset.id, value)" />
              <div class="file-info">
                <span class="file-label" :title="asset.name">{{ asset.name }}</span>
                <el-text type="info" size="small">{{ importedSources.has(asset.id) ? '已导入' : '待导入' }}</el-text>
              </div>
              <el-button link type="primary" :disabled="busy || importing" @click="openSourceSettings(asset)">统计设置</el-button>
              <el-button link type="danger" :disabled="busy" :aria-label="'删除 ' + asset.name" @click="removeAsset(asset)">删除</el-button>
            </div>
            <div v-for="entry in uploadFeedback.data" :key="entry.key" class="survey-file">
              <span class="file-label" :title="entry.name">{{ entry.name }}</span>
              <el-text :type="entry.error ? 'danger' : 'info'" size="small">{{ entry.error || '上传中' }}</el-text>
            </div>
            <el-text v-if="!sources.length && !uploadFeedback.data.length" class="file-empty">暂无答卷</el-text>
          </section>
        </div>
        <div class="toolbar">
          <el-button type="primary" :loading="busy" :disabled="!sourceIds.length || importing" @click="startImport">导入所选答卷</el-button>
          <el-button v-if="catalog" :disabled="busy || importing" @click="tab = 'plan'">前往出报配置</el-button>
          <el-text v-if="catalog">当前版本 {{ project.dataset_id }} · {{ catalog.total.toLocaleString() }} 条答卷</el-text>
          <el-text v-else>待导入</el-text>
        </div>
        <el-alert v-if="project.latest_import?.error" :title="project.latest_import.error" type="error" :closable="false" />
        <el-alert v-else-if="importing" :title="'导入中 ' + (project.latest_import.progress.current || '等待调度')" type="info" :closable="false" />
        <el-dialog :model-value="!!sourceSettings" :title="'统计设置 · ' + (sourceSettings?.name || '')"
                   width="min(800px, 95vw)" :close-on-click-modal="false" :before-close="closeSourceSettings">
            <div v-for="sheet in mappings[sourceSettings?.id] || []" :key="sheet.sheet" class="mapping">
              <h3>{{ sheet.sheet }}</h3>
              <div class="mapping-grid">
                <label>表头行号 <el-input-number :model-value="sheet.header" :min="1" :max="200" :disabled="busy"
                                                   @change="value => changeHeader(sheet, value)" /></label>
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
            <template #footer><el-button type="primary" :disabled="busy" @click="closeSourceSettings()">完成</el-button></template>
        </el-dialog>
        <el-table v-if="catalog?.notices.length" :data="catalog.notices" class="spaced">
          <el-table-column prop="source" label="文件" min-width="200" /><el-table-column prop="issue" label="导入提示" min-width="240" /><el-table-column prop="total" label="数量" width="100" />
        </el-table>
        <el-collapse class="spaced">
          <el-collapse-item title="答卷导入历史">
            <el-table :data="importHistory" empty-text="暂无导入记录">
              <el-table-column label="版本" width="100"><template #default="{ row }">#{{ row.id }}</template></el-table-column>
              <el-table-column prop="source_count" label="文件数" width="90" />
              <el-table-column label="状态" min-width="145"><template #default="{ row }">
                {{ row.current ? '当前版本' : states[row.state] }}<span v-if="row.error"> · {{ row.error }}</span>
              </template></el-table-column>
              <el-table-column label="操作" width="90"><template #default="{ row }">
                <el-button link type="danger" :disabled="busy || ['queued', 'running'].includes(row.state)"
                           :aria-label="'删除导入版本 ' + row.id" @click="removeImport(row)">删除</el-button>
              </template></el-table-column>
            </el-table>
          </el-collapse-item>
        </el-collapse>
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
          <el-table-column label="操作" width="170"><template #default="{ row }">
            <el-button link type="primary" @click="editTemplate(row)">预览与定位</el-button>
            <el-button link type="danger" :disabled="busy" :aria-label="'删除 ' + row.name" @click="removeAsset(row)">删除</el-button>
          </template></el-table-column>
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
        <div class="toolbar">
          <label class="upload"><el-icon><Upload /></el-icon> 上传指标资料
            <input aria-label="上传指标资料" type="file" accept=".xlsx,.docx,.pdf" multiple :disabled="busy" @change="uploadFiles($event, 'indicator')" />
          </label>
        </div>
        <el-table :data="indicatorAssets" empty-text="暂无指标资料">
          <el-table-column prop="name" label="指标资料" min-width="200" />
          <el-table-column label="操作" width="90"><template #default="{ row }">
            <el-button link type="danger" :disabled="busy" :aria-label="'删除 ' + row.name" @click="removeAsset(row)">删除</el-button>
          </template></el-table-column>
        </el-table>
        <el-empty v-if="!catalog" description="请先完成答卷导入" />
        <template v-else>
          <div class="toolbar spaced">
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
              <el-select v-model="orgStage" clearable placeholder="所有学段" aria-label="按学校学段筛选" class="medium-select">
                <el-option v-for="(label, value) in schoolStages" :key="value" :value="value" :label="label" />
              </el-select>
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
              <el-table-column label="组织" min-width="300"><template #default="{ row }"><span :style="{ paddingLeft: levels.findIndex(([key]) => key === row.level) * 9 + 'px' }">{{ displayOrg(row.path, row.school_stage) }}</span></template></el-table-column>
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
              <el-select v-model="selection.levels[key].questionnaires" multiple filterable clearable collapse-tags
                         :aria-label="label + '问卷'" :placeholder="key === 'school' ? '全部适用问卷' : '全部问卷'">
                <el-option v-for="row in catalog.questionnaires" :key="row.id" :value="row.id"
                           :disabled="key === 'school' && !isSchoolQuestionnaireEligible(row)"
                           :label="key === 'school' ? questionnaireLabel(row) : row.name" />
              </el-select>
              <el-select v-model="selection.levels[key].templates" multiple filterable collapse-tags placeholder="本级默认模板" @change="cleanOverrides(key)">
                <el-option v-for="row in levelTemplates(key)" :key="row.id" :value="row.id" :label="row.name + ' #' + row.id" />
              </el-select>
              <el-checkbox v-if="key !== 'province'" v-model="selection.levels[key].split">按问卷分开</el-checkbox>
              <span v-else>合并一份</span>
            </template>
          </div>
          <el-collapse v-if="schoolEligibility" class="spaced">
            <el-collapse-item title="问卷适用学段">
              <el-table :data="catalog.questionnaires" max-height="360">
                <el-table-column prop="name" label="问卷" min-width="235" />
                <el-table-column label="适用学段" width="210"><template #default="{ row }">
                  <span v-if="row.school_stage !== 'any'">{{ schoolStages[row.school_stage] }}</span>
                  <el-select v-else :model-value="selection.questionnaire_stages[row.id] ?? 'any'"
                             :aria-label="row.name + '适用学段'" @change="value => changeQuestionnaireStage(row.id, value)">
                    <el-option value="any" label="按实际答卷" />
                    <el-option value="primary" label="小学" />
                    <el-option value="middle" label="初中" />
                  </el-select>
                </template></el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>
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
            <el-alert v-if="unmatchedSchools" class="spaced" :title="unmatchedSchools + ' 所学校学段未匹配，可上传学校信息补齐'"
                      type="warning" :closable="false" />
            <div class="toolbar spaced"><el-input v-model="previewSearch" clearable :prefix-icon="Search" placeholder="搜索清单中的组织" class="wide-select" /></div>
            <el-table :data="previewPageRows" height="330">
              <el-table-column label="组织" min-width="240"><template #default="{ row }">{{ displayOrg(row.path, row.school_stage) }}</template></el-table-column>
              <el-table-column label="问卷及答卷数" min-width="245"><template #default="{ row }">
                <div v-for="(count, key) in row.samples" :key="key">
                  {{ preview.questionnaires[key] }} · {{ count.toLocaleString() }}
                  <el-text v-if="Object.hasOwn(row.incompatible_questionnaires, key)" type="warning">（学段不适用{{ count ? '，有跨学段答卷' : '' }}）</el-text>
                  <el-text v-else-if="!count" type="info">（无答卷）</el-text>
                </div>
              </template></el-table-column>
              <el-table-column label="Word 模板" min-width="190"><template #default="{ row }">
                <el-select v-if="selection.levels[row.level].templates.length" :model-value="selection.overrides[row.id] ?? row.template_id"
                           @change="value => changeTemplateAssignment(row.id, value)">
                  <el-option v-for="key in selection.levels[row.level].templates" :key="key" :value="key" :label="templateNames[key] + ' #' + key" />
                </el-select>
                <span v-else>本级默认模板</span>
              </template></el-table-column>
              <el-table-column label="生成份数" width="100"><template #default="{ row }">{{ row.reports || '跳过' }}</template></el-table-column>
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
          <el-collapse v-if="batch.generation?.original_issues?.length">
            <el-collapse-item :title="'问卷原稿提示 · ' + batch.generation.original_issues.length + ' 项'">
              <el-table :data="batch.generation.original_issues">
                <el-table-column label="原稿" prop="source" />
                <el-table-column label="处理说明" prop="reason" />
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
            <el-table-column label="组织" min-width="260"><template #default="{ row }">{{ displayOrg(row.org_path, row.school_stage) }}</template></el-table-column>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { inJurisdiction, levels, toggleOrganizations } from '../surveySelection'

const route = useRoute()
const router = useRouter()
const id = computed(() => Number(route.params.id) || null)
const endpoint = computed(() => '/surveys' + (id.value ? '/' + id.value : ''))
const levelNames = Object.fromEntries(levels)
const states = { queued: '排队', running: '运行中', done: '已完成', failed: '失败' }
const schoolStages = { primary: '小学', middle: '初中', unknown: '学段未匹配' }
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
const importHistory = ref([])
const mappings = reactive({})
const sourceSettings = ref(null)
const uploadFeedback = reactive({ data: [], questionnaire: [] })
const sources = computed(() => project.value?.assets.filter(asset => asset.kind === 'data') || [])
const originals = computed(() => project.value?.assets.filter(asset => asset.kind === 'questionnaire') || [])
const importedSources = computed(() => new Set(project.value?.imported_sources || []))
const templates = computed(() => project.value?.assets.filter(asset => asset.kind === 'template') || [])
const indicatorAssets = computed(() => project.value?.assets.filter(asset => asset.kind === 'indicator') || [])
const schoolRoster = computed(() => project.value?.assets.find(asset => asset.kind === 'school'))
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
const orgStage = ref('')
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
const schoolEligibility = computed(() => {
  const chosen = activeOrganizations.value.filter(org => org.level === 'school')
  if (!chosen.length) return null
  const samples = new Map()
  for (const org of chosen) {
    if (!samples.has(org.school_stage)) samples.set(org.school_stage, new Set())
    for (const qid of org.questionnaire_ids) samples.get(org.school_stage).add(qid)
  }
  return samples
})
const configuredLevels = computed(() => {
  const keys = new Set(activeOrganizations.value.map(org => org.level))
  return levels.filter(([key]) => keys.has(key))
})
const visibleOrganizations = computed(() => {
  const keyword = orgSearch.value?.trim().toLowerCase() || ''
  return orgs.value.filter(org =>
    (!orgLevel.value || org.level === orgLevel.value) &&
    (!orgStage.value || org.school_stage === orgStage.value) &&
    (!jurisdiction.value || inJurisdiction(org, jurisdiction.value, orgById.value)) &&
    (!keyword || org.path.toLowerCase().includes(keyword) || org.school_code?.toLowerCase().includes(keyword)))
})
const organizationPage = computed(() => visibleOrganizations.value.slice((orgPage.value - 1) * 30, orgPage.value * 30))
const previewLevels = computed(() => levels.map(([key, label]) => ({ label, ...preview.value?.levels[key] })))
const unmatchedSchools = computed(() => preview.value?.organizations.filter(row => row.school_stage === 'unknown').length || 0)
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
    indicator_id: null, indicator_assets: [], questionnaire_stages: {},
    levels: Object.fromEntries(levels.map(([key]) => [key, { questionnaires: [], templates: [], split: false }])),
  }
}

function displayOrg(path, stage) {
  return stage ? path + '（' + schoolStages[stage] + '）' : path
}

function questionnaireStage(row) {
  return selection.questionnaire_stages[row.id] ?? row.school_stage
}

function isSchoolQuestionnaireEligible(row) {
  const stage = questionnaireStage(row)
  for (const [orgStage, ids] of schoolEligibility.value || []) {
    if ((stage === 'any' || orgStage === 'unknown' || orgStage === stage) && ids.has(row.id)) return true
  }
  return false
}

function questionnaireLabel(row) {
  const stage = schoolStages[questionnaireStage(row)]
  return stage ? row.name + ' · ' + stage : row.name
}

function changeQuestionnaireStage(qid, value) {
  if (value === 'any') delete selection.questionnaire_stages[qid]
  else selection.questionnaire_stages[qid] = value
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

async function confirmDelete(message, title) {
  try {
    await ElMessageBox.confirm(message, title,
      { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' })
    return true
  } catch { return false }
}

async function removeProject(row) {
  const accepted = await confirmDelete(
    `确定删除项目「${row.name}」？上传资料、导入版本和出报结果会一并删除，不可恢复。`, '删除项目')
  if (!accepted) return
  await withBusy(async () => {
    await api.delete('/surveys/' + row.id)
    projects.value = (await api.get('/surveys')).data
    ElMessage.success('已删除项目')
  })
}

async function removeAsset(asset) {
  const impact = asset.kind === 'school'
    ? '学校学段将显示为未匹配。历史报告仍可下载。'
    : importedSources.value.has(asset.id)
      ? '当前答卷版本会清空，请重新选择剩余答卷并导入。历史报告仍可下载。'
      : '该资料将从上传列表移除。历史报告仍可下载。'
  if (!await confirmDelete(`确定删除「${asset.name}」？${impact}`, '删除资料')) return
  await withBusy(async () => {
    await api.delete(endpoint.value + '/assets/' + asset.id)
    preview.value = null
    previewedKey.value = ''
    await loadProject()
    ElMessage.success('已删除资料')
  })
}

async function removeImport(row) {
  const impact = row.current ? '当前出报配置会清空，请重新选择答卷并导入。' : ''
  if (!await confirmDelete(`确定删除导入版本 #${row.id}？${impact}原始答卷和历史报告仍可使用。`, '删除答卷导入版本')) return
  await withBusy(async () => {
    await api.delete(endpoint.value + '/imports/' + row.id)
    await loadProject()
    ElMessage.success('已删除导入版本')
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

async function openSourceSettings(asset) {
  await withBusy(async () => {
    for (const sheet of mappings[asset.id]) {
      const initial = asset.sheets.find(row => row.sheet === sheet.sheet)
      if (sheet.header === initial.header || sheet.loadedHeader === sheet.header) continue
      const { data } = await api.get(endpoint.value + '/assets/' + asset.id + '/headers', {
        params: { sheet: sheet.sheet, header: sheet.header },
      })
      sheet.headers = data.headers
      sheet.loadedHeader = sheet.header
    }
    sourceSettings.value = asset
  })
}

function closeSourceSettings(done) {
  if (busy.value) return
  sourceSettings.value = null
  if (done) done()
}

async function changeHeader(sheet, header) {
  if (!Number.isInteger(header) || header === sheet.header) return
  await withBusy(async () => {
    const { data } = await api.get(endpoint.value + '/assets/' + sourceSettings.value.id + '/headers', {
      params: { sheet: sheet.sheet, header },
    })
    Object.assign(sheet, { ...data, header, loadedHeader: header, questions: {}, editColumn: null })
  })
}

async function linkOriginal(assetId, value) {
  await withBusy(async () => {
    await api.put(endpoint.value + '/questionnaire-originals/' + assetId, {
      dataset_id: project.value.dataset_id, questionnaire_id: value || null,
    })
    await loadProject()
  })
}

async function uploadFiles(event, kind) {
  const files = [...event.target.files]
  event.target.value = ''
  if (!files.length) return
  await withBusy(async () => {
    const target = endpoint.value
    const feedback = uploadFeedback[kind]
    const entries = files.map(file => ({ key: crypto.randomUUID(), name: file.name, error: '' }))
    if (feedback) feedback.splice(0, feedback.length, ...entries)
    for (const [index, file] of files.entries()) {
      const payload = new FormData()
      payload.append('file', file)
      payload.append('kind', kind)
      if (kind === 'template') payload.append('level', uploadLevel.value)
      let data
      try {
        const response = await api.post(target + '/assets', payload, { timeout: 0, silentError: !!feedback })
        data = response.data
      } catch (err) {
        if (!feedback) throw err
        const detail = err.response?.data?.detail
        const entry = feedback.find(row => row.key === entries[index].key)
        if (entry) entry.error = typeof detail === 'string' ? detail : '上传失败，请重试'
        if (endpoint.value !== target) return
        continue
      }
      if (endpoint.value !== target) return
      if (feedback) {
        const position = feedback.findIndex(row => row.key === entries[index].key)
        if (position >= 0) feedback.splice(position, 1)
        const assetIndex = project.value.assets.findIndex(asset => asset.id === data.id)
        if (assetIndex === -1) project.value.assets.push(data)
      }
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
  return { ...clone(selection), dataset_id: project.value?.dataset_id, school_roster_id: project.value?.school_roster_id,
    automatic: true, indicator_id: selection.indicator_id || null }
}

const baseKey = computed(() => {
  const { overrides, ...base } = payload()
  return JSON.stringify(base)
})
watch(baseKey, () => { preview.value = null })
watch([orgSearch, orgLevel, orgStage, jurisdiction], () => { orgPage.value = 1 })
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

let projectLoad = 0
async function loadProject() {
  const current = id.value
  if (!current) return
  const load = ++projectLoad
  const path = '/surveys/' + current
  const [{ data }, history] = await Promise.all([api.get(path), api.get(path + '/imports')])
  if (id.value !== current || load !== projectLoad) return
  const previous = project.value?.dataset_id
  const rosterChanged = project.value?.school_roster_id !== data.school_roster_id
  project.value = data
  importHistory.value = history.data
  const assetIds = new Set(data.assets.map(asset => asset.id))
  sourceIds.value = sourceIds.value.filter(assetId => assetIds.has(assetId))
  for (const key of Object.keys(mappings)) if (!assetIds.has(Number(key))) delete mappings[key]
  if (sourceSettings.value && !assetIds.has(sourceSettings.value.id)) sourceSettings.value = null
  if (templateDraft.value && !assetIds.has(templateDraft.value.id)) {
    templateOpen.value = false
    templateDraft.value = null
  }
  if (indicatorAsset.value && !assetIds.has(indicatorAsset.value)) indicatorAsset.value = null
  selection.indicator_assets = selection.indicator_assets.filter(assetId => assetIds.has(assetId))
  for (const [level] of levels) {
    selection.levels[level].templates = selection.levels[level].templates.filter(assetId => assetIds.has(assetId))
    cleanOverrides(level)
  }
  for (const asset of sources.value) prepareSource(asset, false)
  if (data.dataset_id !== previous) {
    Object.assign(selection, emptySelection())
    if (data.dataset_id) selection.indicator_assets = indicatorAssets.value.map(asset => asset.id)
    catalog.value = null
    questions.value = []
    indicatorDraft.value = []
    for (const key of Object.keys(optionChoices)) delete optionChoices[key]
  }
  if (data.dataset_id && (!catalog.value || rosterChanged)) {
    const responses = await Promise.all(['catalog', 'questions'].map(path =>
      api.get('/surveys/' + current + '/' + path, { params: { dataset_id: data.dataset_id } })))
    if (id.value === current && load === projectLoad) {
      catalog.value = responses[0].data
      questions.value = responses[1].data
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
    const { data, headers } = await api.get(endpoint.value + path, {
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
  return getDownload('/batches/' + batchId.value + '/reports/' + row.id + '/download', {}, '问卷报告.docx')
}

function downloadAnalysis(row) {
  return getDownload('/batches/' + batchId.value + '/reports/' + row.id + '/analysis', {}, '红字解析.docx')
}

function downloadZip(analysis) {
  return getDownload('/batches/' + batchId.value + '/zip', { level: zipLevel.value || undefined, org_id: zipOrg.value || undefined,
    part: zipPart.value, analysis }, analysis ? '红字解析.zip' : '问卷报告.zip')
}

function downloadSchoolTemplate() {
  return getDownload('/school-template', {}, '学校信息模板.xlsx')
}

watch(id, async active => {
  projectLoad++
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
  orgStage.value = ''
  jurisdiction.value = null
  reportState.value = ''
  sourceIds.value = []
  importHistory.value = []
  sourceSettings.value = null
  uploadFeedback.data.splice(0)
  uploadFeedback.questionnaire.splice(0)
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
      }
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
.school-toolbar { align-items: center; padding-bottom: 12px; border-bottom: 1px solid var(--el-border-color-light); }
.school-file { max-width: min(280px, 100%); }
.section-block, .mapping { padding-block: 12px; border-block-start: 1px solid var(--el-border-color-light); margin-top: 12px; }
.upload-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }
.file-panel { min-width: 0; padding: 16px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.panel-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
.survey-file { display: flex; align-items: center; gap: 8px; padding-block: 7px; border-bottom: 1px solid var(--el-border-color-lighter); }
.survey-file:first-of-type { margin-top: 6px; }
.survey-file:last-child { border-bottom: 0; }
.file-info { display: flex; align-items: flex-start; flex-direction: column; flex: 1; min-width: 0; }
.file-info :deep(.el-text) { align-self: flex-start; }
.file-label { display: block; min-width: 0; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }
.file-empty { display: block; margin-top: 10px; }
.original-select { width: 150px; max-width: 46%; flex-shrink: 0; }
@media (max-width: 820px) { .upload-grid { grid-template-columns: minmax(0, 1fr); } }
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
