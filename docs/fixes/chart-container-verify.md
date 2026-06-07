# K线图表容器验证步骤

## 已完成的修复

### 1. 移除template包裹，直接使用div
- 之前：`<template v-else><div id="kline-chart"></div></template>`
- 现在：`<div v-else id="kline-chart" ref="chartContainer"></div>`

### 2. 增加DOM渲染延迟
```javascript
await nextTick()
await new Promise(resolve => setTimeout(resolve, 100))
renderChart(data)
```

### 3. 使用ref优先获取容器
```javascript
let chartDom = chartContainer.value  // 优先使用ref
if (!chartDom) {
  chartDom = document.getElementById('kline-chart')  // 降级使用getElementById
}
```

## 验证步骤

### 1. 刷新页面
在浏览器中按 `F5` 或 `Ctrl+R` 刷新股票详情页

### 2. 查看控制台日志
应该看到：
```
K线数据: {code: "600519", count: 100, data: [...]}
开始渲染图表, 数据条数: 100
图表容器: <div> ref: <div> 高度: 400
ECharts实例创建成功
日期数据示例: (3) ["2024-01-01", "2024-01-02", "2024-01-03"]
K线数据示例: (3) [[1800, 1820, 1790, 1825], ...]
图表配置已应用
```

### 3. 如果仍然显示"图表容器不存在"

在浏览器控制台执行以下诊断代码：

```javascript
(function() {
  console.log('=== 容器诊断 ===')

  // 1. 检查DOM中是否有元素
  const byId = document.getElementById('kline-chart')
  console.log('getElementById结果:', byId)
  console.log('是否存在:', !!byId)
  console.log('可见性:', byId?.offsetParent !== null)

  // 2. 检查Vue组件的ref
  const app = document.querySelector('.stock-detail')
  const vm = app?.__vueParentComponent?.ctx
  console.log('Vue实例:', vm)
  console.log('chartContainer ref:', vm?.chartContainer)

  // 3. 检查容器状态
  if (byId) {
    const styles = window.getComputedStyle(byId)
    console.log('容器样式:', {
      display: styles.display,
      visibility: styles.visibility,
      height: styles.height,
      width: styles.width,
      opacity: styles.opacity,
      zIndex: styles.zIndex
    })
    console.log('容器尺寸:', {
      clientWidth: byId.clientWidth,
      clientHeight: byId.clientHeight,
      offsetWidth: byId.offsetWidth,
      offsetHeight: byId.offsetHeight
    })
  }

  // 4. 检查父元素
  if (byId) {
    let parent = byId.parentElement
    let level = 0
    console.log('父元素链:')
    while (parent && level < 5) {
      const styles = window.getComputedStyle(parent)
      console.log(`  级别${level}: ${parent.tagName}`, {
        display: styles.display,
        visibility: styles.visibility,
        height: styles.height
      })
      parent = parent.parentElement
      level++
    }
  }

  console.log('=== 诊断完成 ===')
})()
```

### 4. 手动测试渲染

如果容器存在但图表不显示，在控制台执行：

```javascript
// 获取Vue组件
const app = document.querySelector('.stock-detail')
const vm = app?.__vueParentComponent?.ctx

// 手动调用渲染
if (vm && vm.klineData?.data) {
  console.log('数据条数:', vm.klineData.data.length)
  console.log('renderChart函数:', typeof vm.renderChart)
  vm.renderChart(vm.klineData.data)
} else {
  console.error('无法获取Vue组件或数据')
}
```

## 常见问题

### 问题1: 容器存在但offsetParent为null

**原因：** 容器被 `display: none` 隐藏了

**解决：** 检查CSS，确保没有隐藏样式
```css
#kline-chart {
  display: block !important;
  visibility: visible !important;
}
```

### 问题2: 容器高度或宽度为0

**原因：** CSS样式问题或父容器尺寸为0

**解决：** 强制设置尺寸
```javascript
const container = document.getElementById('kline-chart')
container.style.width = '100%'
container.style.height = '400px'
container.style.minHeight = '400px'
```

### 问题3: chartContainer.value为null

**原因：** ref还没绑定或组件状态问题

**解决：** 使用getElementById作为备选（代码已实现）

## 如果问题依然存在

请提供以下信息：

1. 完整的控制台日志截图
2. 诊断脚本的所有输出
3. 浏览器类型和版本
4. 股票代码和周期

## 快速测试

访问调试页面验证ECharts本身是否正常：
```
http://localhost:3000/debug-kline.html
```

如果调试页面能看到图表，说明ECharts正常，问题在于主页面的DOM渲染。
