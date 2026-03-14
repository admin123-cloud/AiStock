# 前端应用

StockPy前端是一个基于HTML5 + CSS3 + JavaScript的单页应用（SPA）。

## 项目结构

```
frontend/
├── src/
│   ├── index.html       # 主页面
│   ├── css/
│   │   └── style.css    # 样式表
│   └── js/
│       └── app.js       # 应用脚本
└── package.json         # 项目配置
```

## 快速开始

### 直接打开

在浏览器中打开 `src/index.html` 文件即可。

### 本地服务器

使用Python的内置HTTP服务器：

```bash
cd frontend/src
python -m http.server 3000
```

然后访问 `http://localhost:3000`

或使用Node.js http-server：

```bash
npm install -g http-server
cd frontend/src
http-server -p 3000
```

## 功能模块

### 1. 仪表板（Dashboard）
- 显示今日大盘情绪评分
- 展示上涨/下跌家数
- 特殊涨跌统计（涨幅5%、跌幅5%、涨跌停）
- 成交额信息

### 2. K线分析
- 输入股票代码
- 选择周期（日/周/月）
- 显示K线图表
- 统计数据展示（最高价、最低价、成交量等）

### 3. 情绪监控
- 情绪指数历史趋势图
- 趋势分析（上升/下降）
- 波动性分析
- 历史极值统计

### 4. 回测中心
- 创建回测任务
- 输入策略参数
- 显示回测结果

### 5. WebSocket 实时推送
- 连接到后端WebSocket服务
- 接收实时数据更新
- 自动重连机制

## API集成

前端通过REST API与后端通信：

```javascript
const API_BASE = 'http://localhost:8000/api';
```

### 主要API端点

- `GET /sentiment/today` - 获取今日情绪
- `GET /sentiment/history?days=30` - 历史情绪数据
- `GET /sentiment/analysis/trend?days=30` - 趋势分析
- `GET /kline/list?code=sh000001&period=D` - K线数据
- `GET /health/check` - 健康检查

## 技术特性

- 响应式设计，支持移动设备
- 异步API调用，不阻塞UI
- ECharts 5.4+ 用于数据可视化
- WebSocket 实时连接
- 错误处理和用户提示

## 样式系统

采用CSS变量管理主题颜色：

```css
:root {
  --primary-color: #1890ff;      /* 主色 */
  --success-color: #52c41a;      /* 成功/下跌 */
  --error-color: #f5222d;        /* 错误/上涨 */
  --warning-color: #faad14;      /* 警告 */
}
```

## 布局系统

采用CSS Grid用于响应式布局：

- 仪表板卡片：`grid-template-columns: repeat(auto-fit, minmax(300px, 1fr))`
- 表格数据：响应式3列网格
- 移动设备：自动调整为单列

## 主要函数

### API调用

```javascript
// 获取今日情绪
const sentiment = await StockPyAPI.getTodaySentiment();

// 获取K线数据
const klines = await StockPyAPI.getKlines('sh000001', 'D', 100);

// 获取情绪历史
const history = await StockPyAPI.getSentimentHistory(30);
```

### 数据展示

```javascript
// 显示K线图表
displayKlineChart(klines, code);

// 显示情绪趋势图
displaySentimentChart(sentiments);

// 显示统计数据
displayKlineStats(stats);
```

### 工具函数

```javascript
// 格式化数字
formatNumber(123.456, 2); // "123.46"

// 格式化日期
formatDate(new Date()); // "2024-12-31"

// 显示提示
showAlert('消息内容', 'info|warning|error');
```

## 跨域配置

前端运行在 `http://localhost:3000`，后端运行在 `http://localhost:8000`。

后端已配置CORS支持：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

## 浏览器兼容性

- Chrome 60+
- Firefox 60+
- Safari 12+
- Edge 79+

## 性能优化

- 使用异步加载减少阻塞
- Chart延迟初始化
- WebSocket长连接代替轮询
- 本地缓存常用数据

## 开发指南

### 添加新页面

1. 在 `index.html` 中添加 `<section>` 元素
2. 在 `style.css` 中添加样式
3. 在 `app.js` 中实现逻辑

### 添加新API调用

在 `StockPyAPI` 类中添加方法：

```javascript
static async getNewData(param) {
  return this.fetch(`/new-endpoint?param=${param}`);
}
```

### 添加新图表

使用ECharts库：

```javascript
const chart = echarts.init(document.getElementById('chartId'));
chart.setOption(option);
window.addEventListener('resize', () => chart.resize());
```

## 故障排除

### 连接失败
- 确保后端服务已启动 (`python -m app.main`)
- 检查API地址是否正确
- 查看浏览器控制台的错误信息

### 数据为空
- 检查数据库是否有数据
- 查看网络请求是否成功
- 验证后端API返回数据

### 样式异常
- 清除浏览器缓存
- 刷新页面 (Ctrl+Shift+R)
- 检查CSS文件是否加载

## 部署

### 静态服务器部署

```bash
# 构建：本项目无需构建，直接使用src目录

# Nginx配置
server {
    listen 80;
    server_name example.com;
    
    location / {
        root /path/to/frontend/src;
        try_files $uri /index.html;
    }
    
    location /api {
        proxy_pass http://localhost:8000;
    }
}
```

### Docker部署

```dockerfile
FROM nginx:alpine
COPY frontend/src /usr/share/nginx/html
EXPOSE 80
```

## 许可证

MIT

---

**版本**: 0.1.0
**最后更新**: 2024年12月
