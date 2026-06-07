# AiStock Frontend - 股票监控系统前端

## 项目简介

AiStock前端应用，基于Vue 3开发的股票监控系统界面。

## 技术栈

- **Vue 3** - 渐进式JavaScript框架
- **Vite** - 下一代前端构建工具
- **Vue Router** - 官方路由管理器
- **Pinia** - Vue 3状态管理
- **Element Plus** - Vue 3 UI组件库
- **ECharts** - 数据可视化图表库
- **Axios** - HTTP客户端

## 快速开始

### 1. 安装依赖

```bash
# 进入frontend目录
cd frontend

# 安装依赖
npm install
# 或使用pnpm
pnpm install
```

### 2. 配置后端地址

默认已配置代理到 `http://localhost:8000`

如需修改，编辑 `vite.config.js`：

```javascript
server: {
  proxy: {
    '/api': {
      target: 'http://your-backend-host:port',
      changeOrigin: true
    }
  }
}
```

### 3. 启动开发服务器

```bash
npm run dev
```

访问：http://localhost:3000

### 4. 构建生产版本

```bash
npm run build
```

构建产物在 `dist/` 目录

## 项目结构

```
frontend/
├── public/              # 静态资源
├── src/                 # 源代码
│   ├── api/                  # API接口
│   ├── components/           # 公共组件
│   ├── views/                # 页面组件
│   ├── router/               # 路由配置
│   ├── stores/               # Pinia状态管理
│   ├── utils/                # 工具函数
│   ├── App.vue               # 根组件
│   └── main.js               # 入口文件
├── index.html           # HTML模板
├── vite.config.js       # Vite配置
└── package.json         # 项目配置
```

## 功能模块

### 已规划功能

- [x] 项目基础架构
- [ ] 股票列表展示
- [ ] 股票详情页
- [ ] K线图表
- [ ] 板块监控
- [ ] 市场情绪分析
- [ ] 自选股管理
- [ ] 实时行情推送
- [ ] 技术指标分析

## 开发指南

### 新增页面

1. 在 `src/views/` 创建页面组件
2. 在 `src/router/index.js` 添加路由

### 新增API

在 `src/api/` 中添加API接口：

```javascript
// src/api/stock.js
export function getStockList(params) {
  return request({
    url: '/stocks',
    method: 'get',
    params
  })
}
```

### 使用组件

```vue
<template>
  <el-button type="primary" @click="handleClick">
    按钮
  </el-button>
</template>

<script setup>
import { ElMessage } from 'element-plus'

const handleClick = () => {
  ElMessage.success('点击成功')
}
</script>
```

## 样式规范

- 使用Element Plus主题色
- 响应式设计
- 遵循BEM命名规范

## 代码规范

- 使用Composition API
- 使用`<script setup>`语法糖
- 使用ES6+语法

## 构建部署

### Docker部署

```dockerfile
FROM node:18-alpine as builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
```

### Nginx配置

```nginx
server {
    listen 80;
    server_name localhost;
    
    location / {
        root /usr/share/nginx/html;
        try_files $uri $uri/ /index.html;
    }
    
    location /api {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 浏览器支持

- Chrome >= 87
- Firefox >= 78
- Safari >= 14
- Edge >= 88

## 许可证

MIT License
