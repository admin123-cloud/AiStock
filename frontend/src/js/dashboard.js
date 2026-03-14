/**
 * StockPy 仪表盘逻辑
 * 处理大盘指数数据的获取和展示
 */

const API_BASE = 'http://localhost:8001/api';

// 主要指数代码
const MAJOR_INDICES = {
    sh000001: { name: '上证综指', code: 'sh000001' },
    sh000300: { name: '沪深300', code: 'sh000300' },
    sh000688: { name: '科创50', code: 'sh000688' },
    sz399001: { name: '深证成指', code: 'sz399001' },
    sz399006: { name: '创业板指', code: 'sz399006' }
};

// ======================== 工具函数 ========================

function formatNumber(num, decimals = 2) {
    if (num === null || num === undefined || num === '--') return '--';
    return parseFloat(num).toFixed(decimals);
}

function formatPercent(num, decimals = 2) {
    if (num === null || num === undefined || num === '--') return '--';
    const sign = num >= 0 ? '+' : '';
    return sign + Math.abs(num).toFixed(decimals) + '%';
}

function formatDate(date) {
    if (typeof date === 'string') {
        return date;
    }
    if (date instanceof Date) {
        return date.toISOString().split('T')[0];
    }
    if (date instanceof Date) {
        return new Date().toISOString().split('T')[0];
    }
    return date;
}

function showAlert(message, type = 'info') {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type}`;
    alertDiv.textContent = message;
    document.body.appendChild(alertDiv);
    setTimeout(() => alertDiv.remove(), 3000);
}

// ======================== API 调用类 ========================

class DashboardAPI {
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

    // 获取今日指数数据
    static async getTodayIndices() {
        return this.fetch('/indices/today');
    }

    // 获取指数历史数据
    static async getIndexHistory(code, days = 30) {
        return this.fetch(`/indices/history?code=${code}&days=${days}`);
    }

    // 获取实时指数数据
    static async getRealtimeIndices() {
        return this.fetch('/indices/realtime');
    }

    // 获取今日市场统计
    static async getTodayMarket() {
        return this.fetch('/market/today');
    }

    // 获取市场概况
    static async getMarketSummary(date = null) {
        const params = date ? `?date=${date}` : '';
        return this.fetch(`/market/summary${params}`);
    }

    // 获取最近市场数据
    static async getRecentMarket(days = 7) {
        return this.fetch(`/market/recent?days=${days}`);
    }
}

// ======================== 数据更新函数 ========================

function updateIndexDisplay(code, data) {
    if (!data) {
        console.warn(`指数 ${code} 数据为空`);
        return;
    }

    // 更新指数卡片显示
    const indexConfig = MAJOR_INDICES[code];
    if (!indexConfig) return;

    const valueElement = document.getElementById(`${code}Value`);
    const changeElement = document.getElementById(`${code}Change`);

    if (valueElement) {
        valueElement.textContent = formatNumber(data.close_price);
    }

    if (changeElement) {
        const change = data.change_percent || 0;
        changeElement.textContent = formatPercent(change);
        changeElement.className = `change ${change >= 0 ? 'positive' : 'negative'}`;
    }

    // 更新详细数据
    document.getElementById(`${code}Open`).textContent = formatNumber(data.open_price);
    document.getElementById(`${code}High`).textContent = formatNumber(data.high_price);
    document.getElementById(`${code}Low`).textContent = formatNumber(data.low_price);
    document.getElementById(`${code}Volume`).textContent = formatNumber(data.volume, 0);
    document.getElementById(`${code}Amount`).textContent = formatNumber(data.amount, 2) + '亿';
}

function updateMarketSummary(summary) {
    if (!summary) {
        console.warn('市场统计为空');
        return;
    }

    // 更新涨跌统计
    document.getElementById('upCount').textContent = summary.up_count || '--';
    document.getElementById('downCount').textContent = summary.down_count || '--';
    document.getElementById('flatCount').textContent = summary.flat_count || '--';

    // 更新特殊涨跌
    document.getElementById('up5Percent').textContent = summary.limit_up_count || '--';
    document.getElementById('down5Percent').textContent = summary.limit_down_count || '--';
    document.getElementById('limitUp').textContent = summary.limit_up_count || '--';
    document.getElementById('limitDown').textContent = summary.limit_down_count || '--';

    // 更新成交统计
    document.getElementById('totalAmount').textContent = formatNumber(summary.total_amount / 100000000, 2) + '万亿';
    document.getElementById('totalStocks').textContent = summary.total_stocks || '--';
    document.getElementById('avgChange').textContent = formatPercent((summary.up_count - summary.down_count) / summary.total_stocks * 100);
}

function updateUpdateTime(timestamp) {
    const timeElement = document.getElementById('updateTime');
    if (timeElement && timestamp) {
        const date = new Date(timestamp);
        timeElement.textContent = date.toLocaleString('zh-CN', {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    }
}

// ======================== 数据加载函数 ========================

async function loadDashboardData() {
    try {
        // 显示加载状态
        console.log('开始加载仪表盘数据...');

        // 并行获取指数数据和统计数据
        const [indicesData, marketData] = await Promise.all([
            DashboardAPI.getTodayIndices(),
            DashboardAPI.getTodayMarket()
        ]);

        if (!indicesData || !marketData) {
            showAlert('数据加载失败，请稍后重试', 'error');
            return;
        }

        // 更新日期显示
        if (indicesData.date) {
            const date = new Date(indicesData.date);
            document.getElementById('marketDate').textContent =
                date.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' });
        }

        // 更新各指数数据
        if (indicesData.indices) {
            Object.keys(MAJOR_INDICES).forEach(code => {
                updateIndexDisplay(code, indicesData.indices[code]);
            });
        }

        // 更新市场统计
        if (marketData.market_summary) {
            updateMarketSummary(marketData.market_summary);
        }

        // 更新时间
        updateUpdateTime(new Date().toISOString());

        console.log('仪表盘数据加载完成');
        showAlert('数据刷新完成', 'success');

    } catch (error) {
        console.error('加载仪表盘数据失败:', error);
        showAlert('加载失败，请检查后端服务', 'error');
    }
}

async function refreshData() {
    console.log('刷新仪表盘数据...');
    await loadDashboardData();
}

// ======================== 初始化函数 ========================

async function initDashboard() {
    console.log('初始化仪表盘...');

    // 首次加载数据
    await loadDashboardData();

    // 设置定时刷新（每5分钟）
    setInterval(refreshData, 5 * 60 * 1000);

    console.log('仪表盘初始化完成');
}

// ======================== 页面加载事件 ========================

document.addEventListener('DOMContentLoaded', async function() {
    console.log('仪表盘页面加载...');

    await initDashboard();
});

// ======================== 全局函数 ========================

// 手动刷新函数（暴露给HTML中的onclick事件）
window.refreshData = refreshData;

console.log('仪表盘脚本加载完成');