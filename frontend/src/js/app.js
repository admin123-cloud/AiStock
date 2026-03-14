/**
 * StockPy 前端应用
 * 主应用逻辑和API交互
 */

const API_BASE = 'http://localhost:8000/api';
const WS_URL = 'ws://localhost:8000/ws/market';

// ======================== 工具函数 ========================

function formatDate(date) {
  if (typeof date === 'string') {
    return date;
  }
  if (date instanceof Date) {
    return date.toISOString().split('T')[0];
  }
  return new Date().toISOString().split('T')[0];
}

function formatNumber(num, decimals = 2) {
  if (num === null || num === undefined) return '--';
  return parseFloat(num).toFixed(decimals);
}

function showAlert(message, type = 'info') {
  const alertDiv = document.createElement('div');
  alertDiv.className = `alert alert-${type}`;
  alertDiv.textContent = message;
  document.body.appendChild(alertDiv);
  setTimeout(() => alertDiv.remove(), 3000);
}

// ======================== API 调用 ========================

class StockPyAPI {
  static async fetch(endpoint, options = {}) {
    try {
      const response = await fetch(`${API_BASE}${endpoint}`, {
        mode: 'cors',
        ...options
      });
      
      if (!response.ok) {
        throw new Error(`API错误: ${response.status}`);
      }
      
      return await response.json();
    } catch (error) {
      console.error('API调用失败:', error);
      showAlert(`请求失败: ${error.message}`, 'error');
      return null;
    }
  }

  // 获取今日情绪
  static async getTodaySentiment() {
    return this.fetch('/sentiment/today');
  }

  // 获取情绪历史
  static async getSentimentHistory(days = 30) {
    return this.fetch(`/sentiment/history?days=${days}`);
  }

  // 获取情绪趋势
  static async getSentimentTrend(days = 30) {
    return this.fetch(`/sentiment/analysis/trend?days=${days}`);
  }

  // 获取情绪极值
  static async getSentimentExtremes(days = 365) {
    return this.fetch(`/sentiment/analysis/extremes?days=${days}`);
  }

  // 获取K线数据
  static async getKlines(code, period = 'D', limit = 100) {
    return this.fetch(`/kline/list?code=${code}&period=${period}&limit=${limit}`);
  }

  // 获取K线统计
  static async getKlineStats(code, period = 'D', days = 100) {
    return this.fetch(`/kline/${code}/statistics?period=${period}&days=${days}`);
  }

  // 健康检查
  static async healthCheck() {
    return this.fetch('/health/check');
  }
}

// ======================== 初始化应用 ========================

document.addEventListener('DOMContentLoaded', async function() {
  console.log('应用启动...');
  
  // 检查API连接
  const health = await StockPyAPI.healthCheck();
  if (!health) {
    showAlert('无法连接到后端服务，请确保服务已启动', 'error');
    return;
  }

  // 初始化看板
  initDashboard();
  
  // 初始化K线分析
  initKlineAnalysis();
  
  // 初始化情绪监控
  initSentimentMonitoring();
  
  // 初始化WebSocket
  initWebSocket();

  console.log('应用初始化完成');
});

// ======================== 仪表板初始化 ========================

async function initDashboard() {
  const sentiment = await StockPyAPI.getTodaySentiment();
  
  if (sentiment) {
    document.getElementById('sentimentScore').textContent = 
      formatNumber(sentiment.sentiment_score, 1);
    
    document.getElementById('sentimentLevel').textContent = 
      sentiment.sentiment_level || '未知';
    
    document.getElementById('sentimentUpdate').textContent = 
      `更新时间：${sentiment.date || new Date().toLocaleDateString()}`;
    
    // 更新市场统计
    document.getElementById('upCount').textContent = sentiment.up_count;
    document.getElementById('downCount').textContent = sentiment.down_count;
    document.getElementById('unchangedCount').textContent = sentiment.unchanged_count || '-';
    document.getElementById('turnover').textContent = 
      formatNumber(sentiment.total_turnover_amount) + '亿';
    
    // 更新特殊涨跌
    document.getElementById('up5Percent').textContent = sentiment.up_5_percent_count;
    document.getElementById('down5Percent').textContent = sentiment.down_5_percent_count;
    document.getElementById('limitUp').textContent = sentiment.limit_up_count;
    document.getElementById('limitDown').textContent = sentiment.limit_down_count;
  }
}

// ======================== K线分析初始化 ========================

function initKlineAnalysis() {
  document.getElementById('period').value = 'D';
  
  // 默认加载上证指数
  document.getElementById('stockCode').value = 'sh000001';
}

async function searchKlines() {
  const code = document.getElementById('stockCode').value.trim();
  const period = document.getElementById('period').value;
  
  if (!code) {
    showAlert('请输入股票代码', 'warning');
    return;
  }

  // 获取K线数据
  const klines = await StockPyAPI.getKlines(code, period, 100);
  
  if (!klines || klines.length === 0) {
    showAlert('未找到数据', 'warning');
    return;
  }

  // 显示K线图表
  displayKlineChart(klines, code);
  
  // 显示统计数据
  const stats = await StockPyAPI.getKlineStats(code, period);
  if (stats) {
    displayKlineStats(stats);
  }
}

function displayKlineChart(klines, code) {
  const chart = echarts.init(document.getElementById('klineChart'));
  
  // 处理数据
  const dates = [];
  const opens = [];
  const closes = [];
  const highs = [];
  const lows = [];
  
  klines.reverse().forEach(kline => {
    dates.push(kline.date);
    opens.push(kline.open_price);
    closes.push(kline.close_price);
    highs.push(kline.high_price);
    lows.push(kline.low_price);
  });

  const items = [];
  for (let i = 0; i < dates.length; i++) {
    items.push([opens[i], closes[i], lows[i], highs[i]]);
  }

  const option = {
    title: {
      text: `${code} K线图`,
      left: 'center'
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'cross'
      }
    },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: true,
      splitLine: {
        show: false
      }
    },
    yAxis: {
      scale: true,
      splitArea: {
        show: true
      }
    },
    series: [{
      type: 'candlestick',
      data: items,
      itemStyle: {
        color: '#ec0000',
        color0: '#00da3c',
        borderColor: '#8A0000',
        borderColor0: '#008F28'
      }
    }]
  };

  chart.setOption(option);
  window.addEventListener('resize', () => chart.resize());
}

function displayKlineStats(stats) {
  const html = `
    <div class="stat-item">
      <label>最高收盘：</label>
      <span>${formatNumber(stats.highest_close)}</span>
    </div>
    <div class="stat-item">
      <label>最低收盘：</label>
      <span>${formatNumber(stats.lowest_close)}</span>
    </div>
    <div class="stat-item">
      <label>平均收盘：</label>
      <span>${formatNumber(stats.avg_close)}</span>
    </div>
    <div class="stat-item">
      <label>最高涨幅：</label>
      <span class="up-text">${formatNumber(stats.highest_change)}%</span>
    </div>
    <div class="stat-item">
      <label>最低跌幅：</label>
      <span class="down-text">${formatNumber(stats.lowest_change)}%</span>
    </div>
    <div class="stat-item">
      <label>平均涨幅：</label>
      <span>${formatNumber(stats.avg_change)}%</span>
    </div>
    <div class="stat-item">
      <label>总成交量：</label>
      <span>${formatNumber(stats.total_volume, 0)}</span>
    </div>
    <div class="stat-item">
      <label>总成交额：</label>
      <span>${formatNumber(stats.total_amount / 1e8)}亿</span>
    </div>
  `;
  
  document.getElementById('klineStatistics').innerHTML = html;
}

// ======================== 情绪监控初始化 ========================

async function initSentimentMonitoring() {
  // 获取情绪历史数据
  const sentiments = await StockPyAPI.getSentimentHistory(60);
  
  if (sentiments && sentiments.length > 0) {
    displaySentimentChart(sentiments);
  }

  // 获取趋势分析
  const trend = await StockPyAPI.getSentimentTrend(30);
  const extremes = await StockPyAPI.getSentimentExtremes();

  if (trend) {
    document.getElementById('trendDirection').textContent = trend.trend_direction || '--';
    document.getElementById('volatility').textContent = formatNumber(trend.volatility, 2);
    document.getElementById('trendAverage').textContent = formatNumber(trend.trend_average, 1);
  }

  if (extremes) {
    document.getElementById('highestScore').textContent = formatNumber(extremes.highest_score, 1);
    document.getElementById('lowestScore').textContent = formatNumber(extremes.lowest_score, 1);
  }
}

function displaySentimentChart(sentiments) {
  const chart = echarts.init(document.getElementById('sentimentChart'));
  
  const dates = [];
  const scores = [];
  const levels = [];

  sentiments.forEach(s => {
    dates.push(s.date);
    scores.push(s.sentiment_score);
    levels.push(s.sentiment_level);
  });

  const option = {
    title: {
      text: '大盘情绪指数',
      left: 'center'
    },
    tooltip: {
      trigger: 'axis',
      formatter: function(params) {
        if (params.length > 0) {
          const index = params[0].dataIndex;
          return `${dates[index]}<br/>评分: ${scores[index]}<br/>等级: ${levels[index]}`;
        }
        return '';
      }
    },
    xAxis: {
      type: 'category',
      data: dates
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      splitLine: {
        show: true
      }
    },
    series: [{
      data: scores,
      type: 'line',
      smooth: true,
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(24, 144, 255, 0.3)' },
          { offset: 1, color: 'rgba(24, 144, 255, 0)' }
        ])
      },
      itemStyle: {
        color: '#1890ff'
      },
      markLine: {
        data: [
          { yAxis: 50, name: '中性' },
          { yAxis: 80, name: '极度乐观' },
          { yAxis: 20, name: '极度悲观' }
        ]
      }
    }]
  };

  chart.setOption(option);
  window.addEventListener('resize', () => chart.resize());
}

// ======================== 回测表单处理 ========================

document.addEventListener('DOMContentLoaded', function() {
  const form = document.getElementById('backtestForm');
  if (form) {
    form.addEventListener('submit', async function(e) {
      e.preventDefault();
      
      const strategyName = document.getElementById('strategyName').value;
      const code = document.getElementById('testCode').value;
      const startDate = document.getElementById('startDate').value;
      const endDate = document.getElementById('endDate').value;

      showAlert('回测功能开发中...', 'info');
      console.log('回测参数:', { strategyName, code, startDate, endDate });
    });
  }
});

// ======================== WebSocket 连接 ========================

function initWebSocket() {
  if (!window.WebSocket) {
    console.warn('浏览器不支持WebSocket');
    return;
  }

  const ws = new WebSocket(WS_URL);

  ws.onopen = function() {
    console.log('WebSocket已连接');
  };

  ws.onmessage = function(event) {
    const message = JSON.parse(event.data);
    
    switch(message.type) {
      case 'sentiment_update':
        console.log('情绪更新:', message.data);
        // 可以实时更新仪表板
        break;
      
      case 'kline_update':
        console.log('K线更新:', message.data);
        break;
      
      case 'market_summary':
        console.log('市场摘要:', message.data);
        break;
      
      case 'heartbeat':
        // 心跳，保持连接活跃
        break;
      
      default:
        console.log('未知消息类型:', message.type);
    }
  };

  ws.onerror = function(error) {
    console.error('WebSocket错误:', error);
  };

  ws.onclose = function() {
    console.log('WebSocket连接已关闭');
    // 重新连接
    setTimeout(initWebSocket, 3000);
  };
}

console.log('StockPy 前端应用已加载');
