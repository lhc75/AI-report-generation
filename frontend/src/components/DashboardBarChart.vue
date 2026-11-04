<template>
  <div class="dashboard-bar-chart">
    <el-empty v-if="!normalizedData.length" :description="emptyText" :image-size="72" />
    <div v-show="normalizedData.length" ref="chartEl" class="chart-canvas" :style="{ height: `${height}px` }" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  data: { type: Array, default: () => [] },
  color: { type: String, default: '#409eff' },
  height: { type: Number, default: 300 },
  emptyText: { type: String, default: '暂无数据' },
})

const chartEl = ref(null)
let chart = null
let renderSeq = 0
let resizeObserver = null

const normalizedData = computed(() => (props.data || [])
  .filter((item) => item && item.name !== undefined && item.value !== undefined)
  .map((item) => ({ name: String(item.name), value: Number(item.value) || 0 })))

function disposeChart() {
  if (chart) {
    chart.dispose()
    chart = null
  }
}

async function renderChart() {
  if (!chartEl.value || typeof window === 'undefined') return
  if (!normalizedData.value.length) {
    disposeChart()
    return
  }
  try {
    const echarts = await import('echarts')
    disposeChart()
    chart = echarts.init(chartEl.value)
    const count = normalizedData.value.length
    const dense = count > 15
    const option = {
      animationDuration: 500,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: (value) => `${Number(value || 0).toLocaleString('zh-CN')} 人次`,
      },
      grid: { top: 28, right: 20, bottom: dense ? 56 : 48, left: 62, containLabel: true },
      xAxis: {
        type: 'category',
        data: normalizedData.value.map((item) => item.name),
        axisTick: { alignWithLabel: true },
        axisLabel: {
          interval: 0,
          rotate: count > 6 ? 40 : 0,
          hideOverlap: true,
          fontSize: dense ? 10 : 12,
          color: '#606266',
        },
      },
      yAxis: {
        type: 'value',
        name: '提交人次',
        nameTextStyle: { color: '#909399' },
        axisLabel: { color: '#606266' },
        splitLine: { lineStyle: { color: '#ebeef5' } },
      },
      series: [{
        type: 'bar',
        data: normalizedData.value.map((item) => item.value),
        barMaxWidth: 42,
        itemStyle: { color: props.color, borderRadius: [6, 6, 0, 0] },
        label: {
          show: !dense,
          position: 'top',
          color: '#606266',
          formatter: ({ value }) => Number(value || 0).toLocaleString('zh-CN'),
        },
      }],
    }
    if (count > 30) {
      option.dataZoom = [
        { type: 'inside', start: 0, end: Math.min(100, 3000 / count) },
        { type: 'slider', height: 16, bottom: 8, start: 0, end: Math.min(100, 3000 / count) },
      ]
    }
    chart.setOption(option, true)
    resizeObserver?.observe(chartEl.value)
  } catch {
    disposeChart()
  }
}

watch(normalizedData, async () => {
  const seq = ++renderSeq
  await nextTick()
  if (seq === renderSeq) await renderChart()
}, { deep: true })

onMounted(async () => {
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => chart?.resize())
  }
  await renderChart()
})

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  disposeChart()
})
</script>

<style scoped>
.dashboard-bar-chart { width: 100%; min-height: 120px; }
.chart-canvas { width: 100%; }
</style>
