import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('./views/Login.vue') },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    redirect: '/projects',
    children: [
      { path: 'projects', component: () => import('./views/Projects.vue') },
      { path: 'projects/:id', component: () => import('./views/ProjectDetail.vue') },
      // 暂时关闭数据分析页面
      // { path: 'statistics', component: () => import('./views/DataStatistics.vue') },
      { path: 'ai-config', component: () => import('./views/ModelConfigs.vue') },
      { path: 'skills', component: () => import('./views/Skills.vue') },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) {
    return '/login'
  }
})

export default router

