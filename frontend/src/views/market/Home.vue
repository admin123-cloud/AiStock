<template>
  <div class="home">
    <div class="container">
      <div class="header">
        <h1>大盘情绪概览</h1>
        <div class="header-actions">
          <p class="date">{{ marketData.sentiment?.date || marketData.trading_date || '加载中...' }}</p>
        </div>
      </div>

      <!-- 指数卡片 -->
      <div class="indices-section">
        <div class="section-header">
          <h2 class="section-title">主要指数</h2>
          <button 
            class="more-btn" 
            @click="toggleShowAllIndices"
            :class="{ active: showAllIndices }"
          >
            {{ showAllIndices ? '收起' : '更多' }}
          </button>
        </div>
        <div class="indices-grid" v-if="displayIndices.length > 0">
          <div
            v-for="index in displayIndices"
            :key="index.code"
            class="index-card"
            :class="index.change > 0 ? 'up' : (index.change < 0 ? 'down' : '')"
            @click="goToIndexDetail(index.code)"
            style="cursor: pointer"
          >
            <div class="index-name">{{ index.name }}</div>
            <div class="index-code">{{ index.code }}</div>
            <div class="index-price">{{ formatPrice(index.price) }}</div>
            <div class="index-change">
              <span class="change-value">
                {{ index.change_pct >= 0 ? '+' : '' }}{{ index.change_pct.toFixed(2) }}%
              </span>
              <span class="change-amount">
                {{ index.change >= 0 ? '+' : '' }}{{ index.change.toFixed(2) }}
              </span>
            </div>
            <div class="index-amount">
              成交额: {{ formatAmount(index.amount) }}
            </div>
          </div>
        </div>
        <div class="no-data-card" v-else>
          <div class="no-data-icon">📈</div>
          <div class="no-data-text">暂无指数数据</div>
          <div class="no-data-hint">请在系统配置中获取指数数据</div>
        </div>
      </div>

      <!-- 涨跌统计 -->
      <div class="sentiment-section" v-if="marketData.sentiment">
        <h2 class="section-title">涨跌统计</h2>
        <div class="sentiment-grid">
          <!-- 涨跌家数 -->
          <div class="stat-card">
            <div class="stat-title">涨跌家数</div>
            <div class="stat-content up-down">
              <div class="up-count">
                <span class="count">{{ marketData.sentiment.up_count || 0 }}</span>
                <span class="label">上涨</span>
              </div>
              <div class="unchanged-count">
                <span class="count">{{ marketData.sentiment.unchanged_count || 0 }}</span>
                <span class="label">平盘</span>
              </div>
              <div class="down-count">
                <span class="count">{{ marketData.sentiment.down_count || 0 }}</span>
                <span class="label">下跌</span>
              </div>
            </div>
            <div class="stat-bar">
              <div class="bar-up" :style="{ width: getUpWidth(marketData.sentiment) }"></div>
            </div>
          </div>

          <!-- 涨跌超5% -->
          <div class="stat-card">
            <div class="stat-title">涨跌超5%</div>
            <div class="stat-content up-down">
              <div class="up-count">
                <span class="count">{{ marketData.sentiment.up_5_percent_count || 0 }}</span>
                <span class="label">涨幅>5%</span>
              </div>
              <div class="down-count">
                <span class="count">{{ marketData.sentiment.down_5_percent_count || 0 }}</span>
                <span class="label">跌幅>5%</span>
              </div>
            </div>
          </div>

          <!-- 涨跌停 -->
          <div class="stat-card">
            <div class="stat-title">涨跌停</div>
            <div class="stat-content up-down">
              <div class="up-count limit">
                <span class="count">{{ marketData.sentiment.limit_up_count || 0 }}</span>
                <span class="label">涨停</span>
              </div>
              <div class="down-count limit">
                <span class="count">{{ marketData.sentiment.limit_down_count || 0 }}</span>
                <span class="label">跌停</span>
              </div>
            </div>
          </div>

          <!-- 两市成交 -->
          <div class="stat-card">
            <div class="stat-title">两市成交</div>
            <div class="stat-content">
              <div class="amount-item">
                <span class="label">上证指数</span>
                <span class="amount">{{ formatAmount(marketData.sentiment.sh_amount || 0) }}</span>
              </div>
              <div class="amount-item">
                <span class="label">深证成指</span>
                <span class="amount">{{ formatAmount(marketData.sentiment.sz_amount || 0) }}</span>
              </div>
              <div class="amount-item total">
                <span class="label">合计</span>
                <span class="amount">{{ formatAmount((marketData.sentiment.sh_amount || 0) + (marketData.sentiment.sz_amount || 0)) }}</span>
              </div>
            </div>
          </div>
        </div>

        <div class="sentiment-panel-row">
          <div class="emotion-curve-card" v-if="emotionCurve30d.days.length > 0">
            <div class="curve-card-header">
              <h3 class="curve-card-title">30天情绪曲线</h3>
              <div class="curve-card-latest">
                <span class="curve-snapshot up">{{ marketData.sentiment.up_5_percent_count || 0 }}</span>
                <span class="curve-snapshot down">{{ marketData.sentiment.down_5_percent_count || 0 }}</span>
                <span class="curve-snapshot limit-up">{{ marketData.sentiment.limit_up_count || 0 }}</span>
                <span class="curve-snapshot limit-down">{{ marketData.sentiment.limit_down_count || 0 }}</span>
              </div>
            </div>
            <div class="curve-card-subtitle">涨跌家数 / 涨跌停 / 5%家数按各自30日区间归一化</div>
            <div class="emotion-curve-chart" ref="sentimentCurveChart" @click="selectSentimentPoint"></div>
            <div v-if="selectedSentimentSnapshot" class="sentiment-click-details">
              <div class="sentiment-click-details-header">
                <strong>{{ selectedSentimentSnapshot.date }}</strong>
                <span>当天情绪数据</span>
                <button type="button" @click="clearSelectedSentimentPoint">关闭</button>
              </div>
              <div class="sentiment-click-details-grid">
                <span>上涨 {{ selectedSentimentSnapshot.up }}</span>
                <span>下跌 {{ selectedSentimentSnapshot.down }}</span>
                <span>涨停 {{ selectedSentimentSnapshot.limitUp }}</span>
                <span>跌停 {{ selectedSentimentSnapshot.limitDown }}</span>
                <span>涨幅≥5% {{ selectedSentimentSnapshot.up5 }}</span>
                <span>跌幅≤-5% {{ selectedSentimentSnapshot.down5 }}</span>
              </div>
            </div>
          </div>

          <div class="distribution-wrapper" v-if="sentimentDistribution.length > 0">
            <div class="distribution-card">
              <div class="distribution-header">
                <h3 class="distribution-title">涨跌分布</h3>
                <div class="distribution-summary">
                  <span class="sum-up">{{ marketData.sentiment.up_count || 0 }}</span>
                  <span class="sep">:</span>
                  <span class="sum-flat">{{ marketData.sentiment.unchanged_count || 0 }}</span>
                  <span class="sep">:</span>
                  <span class="sum-down">{{ marketData.sentiment.down_count || 0 }}</span>
                </div>
              </div>
              <div class="distribution-bars">
                <div v-for="item in sentimentDistribution" :key="item.label" class="dist-item">
                  <div class="dist-bar" :class="`dist-${item.side}`" :style="{ height: getDistributionHeight(item.count) }"></div>
                  <div class="dist-label">{{ item.label }}</div>
                  <div class="dist-count">{{ item.count }}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>



      <!-- 情绪周期 -->
      <section class="margin-sentiment-section">
        <div class="section-header">
          <div>
            <h2 class="section-title">融资客情绪</h2>
            <p class="margin-sentiment-note">交易所盘后确认数据，统计沪深两市融资余额与融资净买入</p>
          </div>
          <span v-if="marginSentiment.available" class="margin-sentiment-date">{{ marginSentiment.latest?.date }}</span>
        </div>
        <div v-if="marginSentiment.available" class="margin-sentiment-card">
          <div class="margin-sentiment-metrics">
            <div>
              <span>融资余额</span>
              <strong>{{ formatMarginTrillion(marginSentiment.latest?.financing_balance) }}</strong>
            </div>
            <div>
              <span>融券余额</span>
              <strong>{{ formatMarginBillions(marginSentiment.latest?.securities_lending_balance) }}</strong>
            </div>
            <div :class="marginNetBuyClass">
              <span>融资余额日变动</span>
              <strong>{{ formatMarginBillions(marginSentiment.latest?.financing_net_buy) }}</strong>
            </div>
            <div :class="securitiesLendingChangeClass">
              <span>融券余额日变动</span>
              <strong>{{ formatMarginBillions(marginSentiment.latest?.securities_lending_balance_change) }}</strong>
            </div>
          </div>
          <div class="margin-chart-title"><span>余额趋势</span><span>融资余额 / 融券余额</span></div>
          <svg class="margin-balance-chart" viewBox="0 0 800 150" preserveAspectRatio="none" role="img" aria-label="近30日融资余额和融券余额趋势" @mousemove="updateMarginHover" @mouseleave="clearMarginHover">
            <line x1="36" x2="782" y1="126" y2="126" class="margin-axis-line" />
            <polyline :points="financingBalancePoints" class="financing-balance-line" fill="none" />
            <polyline :points="securitiesLendingBalancePoints" class="securities-lending-line" fill="none" />
            <line v-if="hoveredMarginIndex !== null" :x1="marginHoverX" :x2="marginHoverX" y1="12" y2="126" class="margin-hover-line" />
            <rect x="36" y="12" width="746" height="114" class="margin-hover-surface" />
            <text x="4" y="22">融资</text>
            <text x="764" y="22">融券</text>
          </svg>
          <div class="margin-chart-legend"><span class="financing">融资余额</span><span class="lending">融券余额</span></div>
          <div class="margin-chart-title"><span>每日余额变动</span><span>柱体向上为增加，向下为减少</span></div>
          <svg class="margin-change-chart" viewBox="0 0 800 150" preserveAspectRatio="none" role="img" aria-label="近30日融资和融券余额每日变动" @mousemove="updateMarginHover" @mouseleave="clearMarginHover">
            <line x1="36" x2="782" y1="75" y2="75" class="margin-zero-line" />
            <rect v-for="bar in financingChangeBars" :key="`financing-${bar.index}`" :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height" :class="bar.value >= 0 ? 'margin-bar-up' : 'margin-bar-down'" />
            <rect v-for="bar in securitiesLendingChangeBars" :key="`lending-${bar.index}`" :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height" :class="bar.value >= 0 ? 'lending-bar-up' : 'lending-bar-down'" />
            <line v-if="hoveredMarginIndex !== null" :x1="marginHoverX" :x2="marginHoverX" y1="12" y2="138" class="margin-hover-line" />
            <rect x="36" y="12" width="746" height="126" class="margin-hover-surface" />
            <text x="4" y="22">增加</text>
            <text x="4" y="142">减少</text>
          </svg>
          <div class="margin-chart-legend"><span class="financing">融资变动</span><span class="lending">融券变动</span></div>
          <div class="margin-sentiment-axis">
            <span>{{ marginSentiment.dates?.[0] }}</span>
            <span>{{ marginSentiment.dates?.[marginSentiment.dates.length - 1] }}</span>
          </div>
          <div v-if="marginHoverData" class="margin-hover-tooltip" :style="marginHoverStyle">
            <div class="margin-hover-date">{{ marginHoverData.date }} 当日两融明细</div>
            <div class="margin-hover-grid">
              <span>融资余额 <b>{{ formatMarginTrillion(marginHoverData.financingBalance) }}</b></span>
              <span>融券余额 <b>{{ formatMarginBillions(marginHoverData.securitiesLendingBalance) }}</b></span>
              <span :class="marginHoverData.financingChange >= 0 ? 'positive' : 'negative'">融资日变动 <b>{{ formatMarginBillions(marginHoverData.financingChange) }}</b></span>
              <span :class="marginHoverData.securitiesLendingChange >= 0 ? 'positive' : 'negative'">融券日变动 <b>{{ formatMarginBillions(marginHoverData.securitiesLendingChange) }}</b></span>
              <span>两融余额 <b>{{ formatMarginTrillion(marginHoverData.marginBalance) }}</b></span>
              <span>融资买入额 <b>{{ formatMarginBillions(marginHoverData.financingBuy) }}</b></span>
            </div>
          </div>
        </div>
        <div v-else class="margin-sentiment-empty">两融汇总数据等待收盘后同步</div>
      </section>

      <div class="emotion-cycle-section">
        <div class="section-header">
          <h2 class="section-title">情绪周期</h2>
          <button @click="goToSystemConfigEmotion" class="calculate-btn">
            去系统配置修复
          </button>
        </div>
        <div v-if="currentEmotionPhase" class="emotion-phase-card" :class="`phase-${currentEmotionPhase.phase_code}`">
          <div class="emotion-phase-row">
            <div class="emotion-phase-name">{{ currentEmotionPhase.phase_name }}</div>
            <div class="emotion-phase-confidence">置信度 {{ Math.round((currentEmotionPhase.confidence || 0) * 100) }}%</div>
          </div>
          <div class="emotion-phase-action">{{ currentEmotionPhase.action_hint }}</div>
          <div class="emotion-phase-reason" v-if="currentEmotionPhase.reason_summary">
            {{ currentEmotionPhase.reason_summary }}
          </div>
          <div class="emotion-phase-stale" v-if="currentEmotionPhase.data_status?.is_stale">
            情绪周期日期与行情日期不一致，请先执行“更新最新情绪”。
          </div>
        </div>
        <div v-if="intradayEmotionHint" class="emotion-intraday-note">{{ intradayEmotionHint }}</div>
        <div class="emotion-cycle-chart">
          <div class="emotion-cycle-topbar">
            <div class="emotion-cycle-legend-items">
              <button
                v-for="item in visibleEmotionLegendItems"
                :key="item.key"
                type="button"
                class="emotion-legend-item"
                :class="{ inactive: !emotionLegendSelected[item.name] }"
                @click="toggleEmotionLegend(item.name)"
              >
                <span class="emotion-legend-dot"></span>
                <span class="emotion-legend-text">{{ item.name }}</span>
              </button>
            </div>
            <div class="emotion-topbar-right">
              <div class="emotion-granularity-switch">
                <button
                  type="button"
                  class="granularity-btn"
                  :class="{ active: emotionGranularity === 'daily' }"
                  @click="changeEmotionGranularity('daily')"
                >日线</button>
                <button
                  type="button"
                  class="granularity-btn"
                  :class="{ active: emotionGranularity === 'hourly' }"
                  @click="changeEmotionGranularity('hourly')"
                >小时</button>
              </div>
              <div class="emotion-band-legend">
                <span v-for="band in emotionScoreBands" :key="band.key" class="band-legend-item">
                  <i :style="{ backgroundColor: band.color }"></i>{{ band.label }}
                </span>
              </div>
            </div>
          </div>

          <div v-if="latestEmotionMetrics.length > 0" class="emotion-metric-strip">
            <div
              v-for="item in latestEmotionMetrics"
              :key="item.key"
              class="emotion-metric-chip"
              :style="{ '--metric-color': item.band.color, '--metric-bg': item.band.bg }"
            >
              <span class="metric-name">{{ item.shortName }}</span>
              <span class="metric-value">{{ item.valueText }}</span>
            </div>
          </div>

          <div class="emotion-cycle-canvas" @mouseleave="clearEmotionHover">
            <svg
              v-if="emotionChartModel"
              class="emotion-cycle-svg"
              viewBox="0 0 1100 470"
              preserveAspectRatio="none"
            >
              <defs>
                <clipPath
                  v-for="band in emotionChartModel.bands"
                  :id="`emotion-band-clip-${band.key}`"
                  :key="`clip-${band.key}`"
                  clipPathUnits="userSpaceOnUse"
                >
                  <rect
                    :x="emotionChartModel.padding.left"
                    :y="band.y"
                    :width="emotionChartModel.plotWidth"
                    :height="band.height"
                  />
                </clipPath>
              </defs>

              <rect
                v-for="band in emotionChartModel.bands"
                :key="band.key"
                :x="emotionChartModel.padding.left"
                :y="band.y"
                :width="emotionChartModel.plotWidth"
                :height="band.height"
                :fill="band.bg"
              />

              <g v-for="tick in emotionChartModel.yTicks" :key="`y-${tick}`">
                <line
                  :x1="emotionChartModel.padding.left"
                  :x2="emotionChartModel.width - emotionChartModel.padding.right"
                  :y1="emotionChartModel.yScale(tick)"
                  :y2="emotionChartModel.yScale(tick)"
                  class="emotion-grid-line"
                />
                <text
                  :x="emotionChartModel.padding.left - 10"
                  :y="emotionChartModel.yScale(tick) + 4"
                  class="emotion-axis-label"
                  text-anchor="end"
                >
                  {{ tick }}%
                </text>
              </g>

              <text
                :x="emotionChartModel.padding.left"
                :y="emotionChartModel.padding.top - 12"
                class="emotion-axis-title"
              >
                上涨比例(%)
              </text>

              <g v-for="band in emotionChartModel.bands" :key="`label-${band.key}`">
                <text
                  :x="emotionChartModel.width - emotionChartModel.padding.right - 8"
                  :y="band.y + band.height / 2 + 4"
                  class="emotion-band-label"
                  text-anchor="end"
                >
                  {{ band.label }}
                </text>
              </g>

              <g v-for="label in emotionChartModel.xLabels" :key="`x-${label.index}`">
                <text
                  :x="label.x"
                  :y="emotionChartModel.height - 10"
                  class="emotion-axis-label"
                  text-anchor="middle"
                >
                  {{ label.text }}
                </text>
              </g>

              <g v-for="series in emotionChartModel.series" :key="series.key">
                <path
                  v-for="band in emotionChartModel.bands"
                  :key="`${series.key}-${band.key}`"
                  :d="series.path"
                  fill="none"
                  :stroke="band.color"
                  :stroke-width="series.strokeWidth"
                  :stroke-dasharray="series.dasharray"
                  :clip-path="`url(#emotion-band-clip-${band.key})`"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  class="emotion-series-line"
                />
              </g>

              <g v-if="hoveredEmotionIndex !== null && emotionChartModel.columns[hoveredEmotionIndex]">
                <line
                  :x1="emotionChartModel.columns[hoveredEmotionIndex].x"
                  :x2="emotionChartModel.columns[hoveredEmotionIndex].x"
                  :y1="emotionChartModel.padding.top"
                  :y2="emotionChartModel.height - emotionChartModel.padding.bottom"
                  class="emotion-hover-line"
                />
                <g v-for="point in emotionChartModel.hoverPoints(hoveredEmotionIndex)" :key="`${point.key}-${hoveredEmotionIndex}`">
                  <circle :cx="point.x" :cy="point.y" r="4" :fill="point.color" stroke="#f8fafc" stroke-width="1.4" />
                </g>
              </g>

              <g v-for="column in emotionChartModel.columns" :key="`col-${column.index}`">
                <rect
                  :x="column.left"
                  :y="emotionChartModel.padding.top"
                  :width="column.width"
                  :height="emotionChartModel.plotHeight"
                  fill="transparent"
                  @mouseenter="setEmotionHover(column.index)"
                />
              </g>
            </svg>

            <div v-if="emotionTooltipData" class="emotion-chart-tooltip" :style="emotionTooltipStyle">
              <div class="emotion-tooltip-title">{{ emotionTooltipData.date }}</div>
              <div v-for="item in emotionTooltipData.items" :key="item.key" class="emotion-tooltip-row">
                <span class="emotion-tooltip-marker" :style="{ backgroundColor: item.color }"></span>
                <span class="emotion-tooltip-name">{{ item.name }}</span>
                <span class="emotion-tooltip-value">{{ item.value }}</span>
              </div>
            </div>
          </div>

          <div v-if="emotionPhaseTimeline.length > 0" class="emotion-phase-timeline">
            <div class="phase-timeline-header">
              <span>阶段演化</span>
              <span>{{ emotionPhaseTimelineRange.start }} - {{ emotionPhaseTimelineRange.end }}</span>
            </div>
            <div class="phase-timeline-track" :style="{ gridTemplateColumns: `repeat(${emotionPhaseTimeline.length}, minmax(8px, 1fr))` }">
              <div
                v-for="item in emotionPhaseTimeline"
                :key="item.date"
                class="phase-time-cell"
                :class="`phase-${item.code}`"
                :title="`${item.date} ${item.label} | 收盘上涨比例 ${item.closeUp}% | 强势上涨比例 ${item.strongUp}% | 涨停溢价 ${item.followRate}%`"
              >
                <span v-if="item.showLabel" class="phase-time-label">{{ item.label }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <section class="turnover-trend-section">
        <div class="turnover-trend-header">
          <div>
            <h2 class="section-title">市场成交额趋势</h2>
            <p>两市成交额低于 2 万亿元，说明交易冷清，赚钱难度极大。</p>
          </div>
          <div class="turnover-trend-status" :class="turnoverIsActive ? 'active' : 'cold'">
            {{ latestTurnoverText }}
          </div>
        </div>
        <div v-if="intradayTurnoverForecast.available" class="turnover-forecast-row">
          <span>盘中累计 {{ formatTurnoverTrillion(intradayTurnoverForecast.current_amounts?.total) }}（截至 {{ intradayTurnoverForecast.time_bucket }}）</span>
          <strong>预计全天 {{ formatTurnoverTrillion(intradayTurnoverForecast.forecast_amount) }}</strong>
          <span>区间 {{ formatTurnoverTrillion(intradayTurnoverForecast.forecast_interval?.lower) }} - {{ formatTurnoverTrillion(intradayTurnoverForecast.forecast_interval?.upper) }}</span>
          <span>置信度 {{ Math.round((intradayTurnoverForecast.confidence || 0) * 100) }}%</span>
          <span v-if="intradayTurnoverForecast.fallback">临时估算，5分钟历史样本就绪后自动切换为数据驱动预测</span>
          <span>浅黄虚线段＝预计剩余成交额</span>
        </div>
        <div v-else-if="intradayTurnoverForecast.reason" class="turnover-forecast-row unavailable">
          盘中全天成交额预测：{{ intradayTurnoverForecast.reason }}
        </div>
        <div class="turnover-trend-chart-wrap" @mousemove.capture="handleTurnoverHover" @mouseleave.capture="clearTurnoverHover">
          <div ref="turnoverTrendChart" class="turnover-trend-chart"></div>
          <div v-if="turnoverTooltipData" class="turnover-chart-tooltip" :style="turnoverTooltipStyle">
            <div class="turnover-tooltip-title">{{ turnoverTooltipData.date }}</div>
            <div class="turnover-tooltip-row total">
              <span>三市合计</span>
              <strong>{{ turnoverTooltipData.total }}</strong>
            </div>
            <div v-if="turnoverTooltipData.forecast" class="turnover-tooltip-row forecast">
              <span>预计全天</span>
              <strong>{{ turnoverTooltipData.forecast }}</strong>
            </div>
            <div class="turnover-tooltip-row">
              <span><i class="turnover-tooltip-marker sh"></i>沪市</span>
              <strong>{{ turnoverTooltipData.sh }}</strong>
            </div>
            <div class="turnover-tooltip-row">
              <span><i class="turnover-tooltip-marker sz"></i>深市</span>
              <strong>{{ turnoverTooltipData.sz }}</strong>
            </div>
            <div class="turnover-tooltip-row">
              <span><i class="turnover-tooltip-marker bj"></i>北交所</span>
              <strong>{{ turnoverTooltipData.bj }}</strong>
            </div>
            <div class="turnover-tooltip-change" :class="turnoverTooltipData.changeClass">{{ turnoverTooltipData.changeText }}</div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick, computed } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import * as echarts from '@/utils/charts'

const router = useRouter()

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

const marketData = ref({
  indices: [],
  allIndices: [],
  sentiment: null,
  emotion_cycle: null,
  trading_date: null
})

const loading = ref(false)
const calculating = ref(false)
const sentimentCurveChart = ref(null)
const sentimentCurveInstance = ref(null)
const turnoverTrendChart = ref(null)
const turnoverTrendInstance = ref(null)
const hoveredTurnoverIndex = ref(null)
const turnoverTooltipPosition = ref({ x: 12, y: 12 })
const selectedSentimentIndex = ref(null)
const emotionCycleChart = ref(null)
const emotionCycleData = ref(null)
const marginSentiment = ref({ available: false, dates: [], latest: null })
const hoveredMarginIndex = ref(null)
const marginHoverPosition = ref({ x: 12, y: 12 })
const emotionCycleInstance = ref(null)
const emotionGranularity = ref('daily')
let emotionRefreshTimer = null
let marketDataRetryTimer = null
let marketRefreshTimer = null
const showAllIndices = ref(false)
const emotionLegendPage = ref(0)
const hoveredEmotionIndex = ref(null)
const EMOTION_LEGEND_PAGE_SIZE = 20
const emotionScoreBands = [
  { key: 'ice', label: '冰点', min: 0, max: 20, color: '#39d353', bg: 'rgba(57, 211, 83, 0.10)' },
  { key: 'repair', label: '修复', min: 20, max: 40, color: '#78d64b', bg: 'rgba(120, 214, 75, 0.10)' },
  { key: 'start', label: '启动', min: 40, max: 60, color: '#f3d12f', bg: 'rgba(243, 209, 47, 0.12)' },
  { key: 'ferment', label: '发酵', min: 60, max: 80, color: '#ff7a2f', bg: 'rgba(255, 122, 47, 0.12)' },
  { key: 'hot', label: '高潮', min: 80, max: 100, color: '#ff3b30', bg: 'rgba(255, 59, 48, 0.12)' }
]
const emotionCycleSeriesMeta = [
  { key: 'close_up_rate', name: '收盘上涨比例', shortName: '收盘', color: '#d8dde8', width: 2.2, lineType: 'solid' },
  { key: 'intraday_up_rate', name: '盘中上涨比例', shortName: '盘中', color: '#b8c4d9', width: 2.2, lineType: 'solid' },
  { key: 'sh_up_rate', name: '上证上涨比例', shortName: '上证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'sz_up_rate', name: '深证上涨比例', shortName: '深证', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'cyb_up_rate', name: '创业板上涨比例', shortName: '创业板', color: '#c4cbd8', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_monster_up_rate', name: '上日妖股上涨比例', shortName: '妖股', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_strong_up_rate', name: '上日强势票上涨比例', shortName: '强势', color: '#e2e8f0', width: 2.0, lineType: 'solid' },
  { key: 'yesterday_weak_up_rate', name: '上日弱势票上涨比例', shortName: '弱势', color: '#e2e8f0', width: 2.0, lineType: 'dashed' }
]
const phaseLabelMap = {
  ice: '冰点',
  start: '启动',
  ferment: '发酵',
  euphoria: '高潮',
  decline: '退潮'
}
const phaseCardMeta = {
  ice: {
    phase_name: '冰点期(股灾)',
    action_hint: '按计划分批低吸，等待情绪修复的第一波确认。',
    risk_hint: '优先控制抄底节奏，避免恐慌未收敛前过早重仓。'
  },
  start: {
    phase_name: '启动期',
    action_hint: '轻仓试错，优先跟踪开始转强的核心方向。',
    risk_hint: '信号仍在构建，避免一次性重仓。'
  },
  ferment: {
    phase_name: '发酵期',
    action_hint: '持股为主，围绕主线做结构优化。',
    risk_hint: '注意分化，不要在同一方向过度堆叠。'
  },
  euphoria: {
    phase_name: '高潮期',
    action_hint: '分批止盈，降低追高频率，保留核心强势仓位。',
    risk_hint: '高波动区间，提防情绪反转和高位回撤。'
  },
  decline: {
    phase_name: '退潮期',
    action_hint: '控制回撤，减仓弱势品种，等待下一轮信号。',
    risk_hint: '退潮时容易连续回撤，避免频繁交易。'
  }
}
const emotionLegendSelected = ref(
  Object.fromEntries(emotionCycleSeriesMeta.map((item) => [item.name, true]))
)

// 计算属性：显示的指数列表
const displayIndices = computed(() => {
  if (showAllIndices.value) {
    return marketData.value.allIndices
  } else {
    // 主要指数列表
    const mainIndices = ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]
    return marketData.value.allIndices.filter(index => mainIndices.includes(index.code))
  }
})

const sentimentDistribution = computed(() => {
  const raw = marketData.value?.sentiment?.change_distribution
  if (!Array.isArray(raw)) return []
  return raw.map((item) => ({
    label: item.label,
    count: Number(item.count || 0),
    side: item.side || 'flat'
  }))
})

const emotionCurve30d = computed(() => {
  const curve = marketData.value?.sentiment?.emotion_curve_30d
  if (!curve || !Array.isArray(curve.trade_dates)) {
    return { days: [], raw: {}, normalized: {}, latest: {} }
  }
  return {
    days: curve.trade_dates,
    raw: curve.raw_series || {},
    normalized: curve.normalized_series || {},
    latest: curve.latest_snapshot || {}
  }
})

const selectedSentimentSnapshot = computed(() => {
  const index = selectedSentimentIndex.value
  const { days, raw } = emotionCurve30d.value
  if (!Number.isInteger(index) || index < 0 || index >= days.length) return null
  const value = (key) => Number(raw[key]?.[index] || 0)
  return {
    date: days[index],
    up: value('up_count_series'),
    down: value('down_count_series'),
    limitUp: value('limit_up_count_series'),
    limitDown: value('limit_down_count_series'),
    up5: value('up_5_count_series'),
    down5: value('down_5_count_series')
  }
})

const turnoverTrend = computed(() => {
  const raw = marketData.value?.sentiment?.turnover_trend_30d || {}
  return {
    days: Array.isArray(raw.trade_dates) ? raw.trade_dates : [],
    amounts: Array.isArray(raw.amounts) ? raw.amounts.map((value) => Number(value || 0)) : [],
    shAmounts: Array.isArray(raw.sh_amounts) ? raw.sh_amounts.map((value) => Number(value || 0)) : [],
    szAmounts: Array.isArray(raw.sz_amounts) ? raw.sz_amounts.map((value) => Number(value || 0)) : [],
    bjAmounts: Array.isArray(raw.bj_amounts) ? raw.bj_amounts.map((value) => Number(value || 0)) : [],
    threshold: Number(raw.threshold_amount || 2_000_000_000_000),
    latestIsProvisional: Boolean(raw.latest_is_provisional),
    latestAsOf: raw.latest_as_of || ''
  }
})

const intradayTurnoverForecast = computed(() => (
  marketData.value?.sentiment?.turnover_forecast_intraday || { available: false, reason: '' }
))

const formatMarginTrillion = (amount) => `${(Number(amount || 0) / 1_000_000_000_000).toFixed(2)} 万亿`
const formatMarginBillions = (amount) => {
  const value = Number(amount || 0) / 100_000_000
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)} 亿`
}
const marginNetBuyClass = computed(() => Number(marginSentiment.value.latest?.financing_net_buy || 0) >= 0 ? 'positive' : 'negative')
const securitiesLendingChangeClass = computed(() => Number(marginSentiment.value.latest?.securities_lending_balance_change || 0) >= 0 ? 'positive' : 'negative')
const marginBalancePoints = (values) => {
  if (!values.length) return ''
  const minimum = Math.min(...values)
  const maximum = Math.max(...values)
  const range = Math.max(maximum - minimum, 1)
  const width = 746
  return values.map((value, index) => {
    const x = 36 + (values.length === 1 ? width / 2 : index * width / (values.length - 1))
    const y = 118 - (Number(value || 0) - minimum) / range * 94
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}
const financingBalancePoints = computed(() => marginBalancePoints((marginSentiment.value.financing_balance || []).map(Number)))
const securitiesLendingBalancePoints = computed(() => marginBalancePoints((marginSentiment.value.securities_lending_balance || []).map(Number)))
const marginChangeBars = (values, offset) => {
  if (!values.length) return []
  const maxAbs = Math.max(...values.map((value) => Math.abs(Number(value || 0))), 1)
  const step = 746 / values.length
  const width = Math.max(2, Math.min(8, step * 0.32))
  return values.map((value, index) => {
    const amount = Number(value || 0)
    const height = Math.abs(amount) / maxAbs * 57
    return { index, value: amount, x: 36 + index * step + offset * width, y: amount >= 0 ? 75 - height : 75, width, height }
  })
}
const financingChangeBars = computed(() => marginChangeBars(marginSentiment.value.financing_net_buy || [], 0))
const securitiesLendingChangeBars = computed(() => marginChangeBars(marginSentiment.value.securities_lending_balance_change || [], 1.15))
const marginHoverX = computed(() => {
  const count = marginSentiment.value.dates?.length || 0
  if (hoveredMarginIndex.value === null || !count) return 36
  return 36 + (count === 1 ? 373 : hoveredMarginIndex.value * 746 / (count - 1))
})
const marginHoverData = computed(() => {
  const index = hoveredMarginIndex.value
  const dates = marginSentiment.value.dates || []
  if (!Number.isInteger(index) || !dates[index]) return null
  const value = (key) => Number(marginSentiment.value[key]?.[index] || 0)
  return {
    date: dates[index],
    financingBalance: value('financing_balance'),
    securitiesLendingBalance: value('securities_lending_balance'),
    financingChange: value('financing_net_buy'),
    securitiesLendingChange: value('securities_lending_balance_change'),
    marginBalance: value('margin_balance'),
    financingBuy: value('financing_buy')
  }
})
const marginHoverStyle = computed(() => ({
  left: `${marginHoverPosition.value.x}px`,
  top: `${marginHoverPosition.value.y}px`
}))
const updateMarginHover = (event) => {
  const dates = marginSentiment.value.dates || []
  if (!dates.length) return
  const svgRect = event.currentTarget.getBoundingClientRect()
  const chartRatio = Math.max(0, Math.min(1, (event.clientX - svgRect.left - svgRect.width * 0.045) / (svgRect.width * 0.9325)))
  hoveredMarginIndex.value = Math.round(chartRatio * (dates.length - 1))
  const cardRect = event.currentTarget.closest('.margin-sentiment-card').getBoundingClientRect()
  marginHoverPosition.value = {
    x: Math.max(8, Math.min(cardRect.width - 262, event.clientX - cardRect.left + 14)),
    y: Math.max(8, Math.min(cardRect.height - 142, event.clientY - cardRect.top + 14))
  }
}
const clearMarginHover = () => {
  hoveredMarginIndex.value = null
}

const formatTurnoverTrillion = (amount) => `${(Number(amount || 0) / 1_000_000_000_000).toFixed(2)} 万亿`

const turnoverTooltipData = computed(() => {
  const index = hoveredTurnoverIndex.value
  if (index === null || !turnoverTrend.value.days[index]) return null
  const total = Number(turnoverTrend.value.amounts[index] || 0)
  const previous = index > 0 ? Number(turnoverTrend.value.amounts[index - 1] || 0) : null
  const change = previous === null ? 0 : total - previous
  const changePercent = previous ? change / previous * 100 : 0
  const isLatestIntraday = index === turnoverTrend.value.days.length - 1 && intradayTurnoverForecast.value.available
  return {
    date: turnoverTrend.value.days[index],
    total: formatTurnoverTrillion(total),
    sh: formatTurnoverTrillion(turnoverTrend.value.shAmounts[index]),
    sz: formatTurnoverTrillion(turnoverTrend.value.szAmounts[index]),
    bj: formatTurnoverTrillion(turnoverTrend.value.bjAmounts[index]),
    forecast: isLatestIntraday ? formatTurnoverTrillion(intradayTurnoverForecast.value.forecast_amount) : '',
    changeClass: change > 0 ? 'up' : (change < 0 ? 'down' : 'flat'),
    changeText: previous === null
      ? '首个统计日，无前日对比'
      : `较前日${change > 0 ? '放量' : (change < 0 ? '缩量' : '持平')} ${formatTurnoverTrillion(Math.abs(change))}（${change > 0 ? '+' : ''}${changePercent.toFixed(2)}%）`
  }
})

const turnoverTooltipStyle = computed(() => ({
  left: `${turnoverTooltipPosition.value.x}px`,
  top: `${turnoverTooltipPosition.value.y}px`
}))

const latestTurnoverAmount = computed(() => {
  const amounts = turnoverTrend.value.amounts
  return amounts.length ? amounts[amounts.length - 1] : 0
})

const turnoverIsActive = computed(() => latestTurnoverAmount.value >= turnoverTrend.value.threshold)

const latestTurnoverText = computed(() => {
  if (!latestTurnoverAmount.value) return '成交额数据待更新'
  const amountInTrillion = latestTurnoverAmount.value / 1_000_000_000_000
  if (turnoverTrend.value.latestIsProvisional) {
    const asOf = String(turnoverTrend.value.latestAsOf).slice(11, 16)
    return `${amountInTrillion.toFixed(2)} 万亿 · 盘中累计${asOf ? `（截至 ${asOf}）` : ''}`
  }
  return `${amountInTrillion.toFixed(2)} 万亿 · ${turnoverIsActive.value ? '交易活跃' : '交易冷清'}`
})

const emotionLegendPageCount = computed(() => (
  Math.max(1, Math.ceil(emotionCycleSeriesMeta.length / EMOTION_LEGEND_PAGE_SIZE))
))

const visibleEmotionLegendItems = computed(() => {
  const start = emotionLegendPage.value * EMOTION_LEGEND_PAGE_SIZE
  return emotionCycleSeriesMeta.slice(start, start + EMOTION_LEGEND_PAGE_SIZE)
})

const activeEmotionSeriesMeta = computed(() => (
  emotionCycleSeriesMeta.filter((item) => emotionLegendSelected.value[item.name] !== false)
))

const getEmotionScoreBand = (value) => {
  const score = Math.max(0, Math.min(100, Number(value) || 0))
  return emotionScoreBands.find((item) => score >= item.min && score <= item.max) || emotionScoreBands[0]
}

const latestEmotionMetrics = computed(() => {
  const ec = emotionCycleData.value
  const dates = ec?.dates || []
  if (!dates.length) return []
  const latestIndex = dates.length - 1
  return emotionCycleSeriesMeta.map((meta) => {
    const value = Number(ec?.[meta.key]?.[latestIndex] ?? 0)
    return {
      ...meta,
      value,
      valueText: value.toFixed(2) + '%',
      band: getEmotionScoreBand(value)
    }
  })
})

const buildSmoothPath = (points) => {
  if (!points.length) return ''
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`

  let d = `M ${points[0].x} ${points[0].y}`
  for (let i = 0; i < points.length - 1; i += 1) {
    const current = points[i]
    const next = points[i + 1]
    const previous = points[i - 1] || current
    const afterNext = points[i + 2] || next

    const cp1x = current.x + (next.x - previous.x) / 6
    const cp1y = current.y + (next.y - previous.y) / 6
    const cp2x = next.x - (afterNext.x - current.x) / 6
    const cp2y = next.y - (afterNext.y - current.y) / 6

    d += ` C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${next.x} ${next.y}`
  }

  return d
}

const emotionChartModel = computed(() => {
  const ec = emotionCycleData.value
  const dates = ec?.dates || []
  const seriesMeta = activeEmotionSeriesMeta.value
  if (!dates.length || !seriesMeta.length) return null

  const width = 1100
  const height = 470
  const padding = { top: 30, right: 14, bottom: 40, left: 50 }
  const plotWidth = width - padding.left - padding.right
  const plotHeight = height - padding.top - padding.bottom
  const maxIndex = Math.max(dates.length - 1, 1)
  const xScale = (index) => padding.left + (plotWidth * index) / maxIndex
  const yScale = (value) => padding.top + ((100 - Math.max(0, Math.min(100, Number(value) || 0))) / 100) * plotHeight
  const labelStep = Math.max(1, Math.ceil(dates.length / 7))

  const bands = emotionScoreBands.map((band) => {
    const upperY = yScale(band.max)
    const lowerY = yScale(band.min)
    return {
      ...band,
      y: upperY,
      height: Math.max(1, lowerY - upperY)
    }
  })
  const series = seriesMeta.map((meta) => {
    const values = dates.map((_, index) => Number(ec[meta.key]?.[index] ?? 0))
    const points = values.map((value, index) => ({
      x: xScale(index),
      y: yScale(value),
      value,
      band: getEmotionScoreBand(value)
    }))
    return {
      key: meta.key,
      name: meta.name,
      color: meta.color,
      values,
      strokeWidth: meta.width,
      dasharray: meta.lineType === 'dashed' ? '8 6' : undefined,
      points,
      path: buildSmoothPath(points)
    }
  })

  const columns = dates.map((_, index) => {
    const x = xScale(index)
    const prevX = index === 0 ? padding.left : xScale(index - 1)
    const nextX = index === dates.length - 1 ? width - padding.right : xScale(index + 1)
    return {
      index,
      x,
      left: index === 0 ? padding.left : (prevX + x) / 2,
      width: index === dates.length - 1 ? Math.max(16, width - padding.right - ((prevX + x) / 2)) : Math.max(16, (nextX - prevX) / 2)
    }
  })

  const xLabels = dates
    .map((date, index) => ({ date, index, x: xScale(index) }))
    .filter((item, index) => index === 0 || index === dates.length - 1 || index % labelStep === 0)
    .map((item) => ({ ...item, text: formatTrendDate(item.date) }))

  return {
    width,
    height,
    padding,
    plotWidth,
    plotHeight,
    bands,
    yTicks: [0, 20, 40, 60, 80, 100],
    dates,
    series,
    columns,
    xLabels,
    xScale,
    yScale,
    hoverPoints: (index) => series.map((item) => {
      const value = item.values[index] ?? 0
      return {
        key: item.key,
        color: getEmotionScoreBand(value).color,
        x: xScale(index),
        y: yScale(value)
      }
    })
  }
})

const emotionTooltipData = computed(() => {
  if (hoveredEmotionIndex.value === null || !emotionChartModel.value) return null
  const index = hoveredEmotionIndex.value
  const date = emotionChartModel.value.dates[index]
  if (!date) return null
  return {
    date: formatTrendDateCompact(date),
    items: emotionChartModel.value.series.map((series) => {
        const value = series.values[index] ?? 0
        return {
          key: series.key,
          name: series.name,
          color: getEmotionScoreBand(value).color,
          value: value.toFixed(2) + '%'
        }
      })
  }
})

const emotionTooltipStyle = computed(() => {
  if (hoveredEmotionIndex.value === null || !emotionChartModel.value) return {}
  const column = emotionChartModel.value.columns[hoveredEmotionIndex.value]
  if (!column) return {}
  const ratio = emotionChartModel.value.width <= 0 ? 0 : column.x / emotionChartModel.value.width
  return {
    left: ratio > 0.62
      ? `clamp(16px, calc(${(ratio * 100).toFixed(2)}% - 210px), calc(100% - 186px))`
      : `clamp(16px, calc(${(ratio * 100).toFixed(2)}% + 12px), calc(100% - 186px))`,
    top: '22px'
  }
})

const resolveEmotionPhaseCode = (point, prevPoint) => {
  const closeUp = Number(point.closeUp || 0)
  const strongUp = Number(point.strongUp || 0)
  const followRate = Number(point.followRate || 0)
  const prevCloseUp = Number(prevPoint?.closeUp ?? closeUp)
  const prevStrongUp = Number(prevPoint?.strongUp ?? strongUp)
  const deltaCloseUp = closeUp - prevCloseUp
  const deltaStrongUp = strongUp - prevStrongUp

  if (closeUp <= 38 || (closeUp <= 45 && strongUp <= 15)) return 'ice'
  if (closeUp >= 72 && strongUp >= 35 && followRate >= 58) return 'euphoria'
  if ((deltaCloseUp <= -5 && closeUp < 62) || (strongUp < 20 && deltaStrongUp <= -2)) return 'decline'
  if (closeUp >= 56 && strongUp >= 24 && deltaCloseUp >= -1) return 'ferment'
  return 'start'
}

const resolveEmotionPhaseDetail = (point, prevPoint, granularity, dataStatus = null) => {
  const closeUp = Number(point.closeUp || 0)
  const strongUp = Number(point.strongUp || 0)
  const followRate = Number(point.followRate || 0)
  const prevCloseUp = Number(prevPoint?.closeUp ?? closeUp)
  const prevStrongUp = Number(prevPoint?.strongUp ?? strongUp)
  const deltaCloseUp = closeUp - prevCloseUp
  const deltaStrongUp = strongUp - prevStrongUp
  const code = resolveEmotionPhaseCode(point, prevPoint)
  const meta = phaseCardMeta[code] || phaseCardMeta.start
  const reasons = []
  let confidenceHits = 0

  if (code === 'ice') {
    if (closeUp <= 38) {
      reasons.push(`收盘上涨比例偏低(${closeUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (strongUp <= 15) {
      reasons.push(`强势上涨比例偏低(${strongUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (deltaCloseUp <= 0) {
      reasons.push(`短期动量仍未转强(${deltaCloseUp.toFixed(1)})`)
      confidenceHits += 1
    }
  } else if (code === 'euphoria') {
    if (closeUp >= 72) {
      reasons.push(`收盘上涨比例高位(${closeUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (strongUp >= 35) {
      reasons.push(`强势上涨比例较高(${strongUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (followRate >= 58) {
      reasons.push(`涨停溢价较好(${followRate.toFixed(1)}%)`)
      confidenceHits += 1
    }
  } else if (code === 'decline') {
    if (deltaCloseUp <= -5) {
      reasons.push(`收盘上涨比例回落(${deltaCloseUp.toFixed(1)})`)
      confidenceHits += 1
    }
    if (strongUp < 20) {
      reasons.push(`强势上涨比例偏弱(${strongUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (deltaStrongUp <= -2) {
      reasons.push(`强势动量走弱(${deltaStrongUp.toFixed(1)})`)
      confidenceHits += 1
    }
  } else if (code === 'ferment') {
    if (closeUp >= 56) {
      reasons.push(`收盘上涨比例维持强势(${closeUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (strongUp >= 24) {
      reasons.push(`强势上涨比例抬升(${strongUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (deltaCloseUp >= -1) {
      reasons.push(`短期动量保持稳定(${deltaCloseUp.toFixed(1)})`)
      confidenceHits += 1
    }
  } else {
    if (closeUp >= 42 && closeUp <= 58) {
      reasons.push(`收盘上涨比例进入修复区间(${closeUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
    if (deltaCloseUp >= 2) {
      reasons.push(`短期动量明显改善(${deltaCloseUp.toFixed(1)})`)
      confidenceHits += 1
    }
    if (strongUp >= 18) {
      reasons.push(`强势表现开始回升(${strongUp.toFixed(1)}%)`)
      confidenceHits += 1
    }
  }

  if (!reasons.length) {
    reasons.push(`${granularity === 'hourly' ? '小时' : '日线'}情绪曲线当前处于中间区间`)
  }

  return {
    phase_code: code,
    phase_name: meta.phase_name,
    confidence: Math.min(0.95, 0.48 + confidenceHits * 0.12),
    action_hint: meta.action_hint,
    risk_hint: meta.risk_hint,
    reason_summary: reasons.join('；'),
    data_status: dataStatus || undefined
  }
}

const currentEmotionPhase = computed(() => {
  const ec = emotionCycleData.value
  const dates = ec?.dates || []
  if (!dates.length) {
    return marketData.value.sentiment?.emotion_phase || null
  }

  const lastIndex = dates.length - 1
  const point = {
    closeUp: ec?.close_up_rate?.[lastIndex] ?? 0,
    strongUp: ec?.strong_up_rate?.[lastIndex] ?? 0,
    followRate: ec?.limit_up_follow_rate?.[lastIndex] ?? 0
  }
  const prevPoint = lastIndex > 0
    ? {
        closeUp: ec?.close_up_rate?.[lastIndex - 1] ?? point.closeUp,
        strongUp: ec?.strong_up_rate?.[lastIndex - 1] ?? point.strongUp,
        followRate: ec?.limit_up_follow_rate?.[lastIndex - 1] ?? point.followRate
      }
    : point

  const fallbackStatus = emotionGranularity.value === 'daily'
    ? (ec?.intraday_snapshot
        ? {
            emotion_cycle_date: ec.intraday_snapshot.date,
            trade_date: ec.intraday_snapshot.date,
            is_stale: false,
            is_temporary: true
          }
        : marketData.value.sentiment?.emotion_phase?.data_status)
    : null

  return resolveEmotionPhaseDetail(point, prevPoint, emotionGranularity.value, fallbackStatus)
})

const emotionPhaseTimeline = computed(() => {
  const ec = emotionCycleData.value
  const dates = ec?.dates || []
  if (!dates.length) return []

  const closeSeries = ec?.close_up_rate || []
  const strongSeries = ec?.strong_up_rate || []
  const followSeries = ec?.limit_up_follow_rate || []
  const size = dates.length
  const timeline = []
  const labelStep = Math.max(1, Math.floor(size / 6))

  for (let i = 0; i < size; i += 1) {
    const point = {
      closeUp: closeSeries[i] ?? 0,
      strongUp: strongSeries[i] ?? 0,
      followRate: followSeries[i] ?? 0
    }
    const prevPoint = i > 0
      ? {
          closeUp: closeSeries[i - 1] ?? point.closeUp,
          strongUp: strongSeries[i - 1] ?? point.strongUp,
          followRate: followSeries[i - 1] ?? point.followRate
        }
      : point
    const detail = resolveEmotionPhaseDetail(point, prevPoint, emotionGranularity.value)
    timeline.push({
      date: dates[i],
      dateLabel: formatTrendDate(dates[i]),
      code: detail.phase_code,
      label: phaseLabelMap[detail.phase_code] || '启动',
      showLabel: i === 0 || i === size - 1 || i % labelStep === 0,
      closeUp: Number(point.closeUp || 0).toFixed(1),
      strongUp: Number(point.strongUp || 0).toFixed(1),
      followRate: Number(point.followRate || 0).toFixed(1)
    })
  }
  return timeline
})

const emotionPhaseTimelineRange = computed(() => {
  const list = emotionPhaseTimeline.value
  if (!list.length) return { start: '--', end: '--' }
  return {
    start: list[0].dateLabel,
    end: list[list.length - 1].dateLabel
  }
})

// 切换显示所有指数
const toggleShowAllIndices = async () => {
  showAllIndices.value = !showAllIndices.value
  if (showAllIndices.value && marketData.value.allIndices.length === 0) {
    // 如果切换到显示所有指数但还没有加载，就加载所有指数
    await loadAllIndices()
  }
}

// 加载所有指数数据
const loadAllIndices = async () => {
  try {
    const response = await axios.get(`${API_BASE}/market/indices`, {
      params: { all: true, _ts: Date.now() }
    })
    marketData.value.allIndices = response.data.indices || []
  } catch (error) {
    console.error('加载所有指数失败:', error)
  }
}

// 加载大盘数据
const loadMarketData = async (retryCount = 0) => {
  loading.value = true
  try {
    const refreshParams = { _ts: Date.now() }
    // 并行请求指数数据和涨跌统计数据
    const [indicesRes, sentimentRes, marginRes] = await Promise.all([
      axios.get(`${API_BASE}/market/indices`, { params: { all: true, ...refreshParams } }),
      axios.get(`${API_BASE}/market/sentiment`, { params: refreshParams }),
      axios.get(`${API_BASE}/market/margin-sentiment`, { params: { days: 30, ...refreshParams } })
    ])

    marginSentiment.value = marginRes.data || { available: false, dates: [], latest: null }

    marketData.value = {
      indices: indicesRes.data.indices || [],
      allIndices: indicesRes.data.indices || [],
      sentiment: sentimentRes.data.error ? null : sentimentRes.data,
      emotion_cycle: null,
      trading_date: indicesRes.data.trading_date || null
    }
    
    // 打印日志，方便调试
    console.log('涨跌统计数据:', sentimentRes.data)

    if (marketData.value.sentiment?.emotion_curve_30d?.trade_dates?.length) {
      await nextTick()
      setTimeout(() => renderSentimentCurveChart(), 60)
    }
    if (turnoverTrend.value.days.length) {
      await nextTick()
      setTimeout(() => renderTurnoverTrendChart(), 60)
    }

    // 加载情绪周期数据
    await loadEmotionCycle()

  } catch (error) {
    console.error('加载大盘数据失败:', error)
    // 前后端容器同时重启时，前端可能比后端更早就绪；短暂重试避免首页图表永久空白。
    if (retryCount < 3) {
      if (marketDataRetryTimer) window.clearTimeout(marketDataRetryTimer)
      marketDataRetryTimer = window.setTimeout(() => {
        marketDataRetryTimer = null
        loadMarketData(retryCount + 1)
      }, 2_000)
    }
  } finally {
    loading.value = false
  }
}

// 加载情绪周期数据
const loadEmotionCycle = async () => {
  try {
    const response = await axios.get(`${API_BASE}/market/emotion-trend`, {
      params: { days: 30, granularity: emotionGranularity.value }
    })
    emotionCycleData.value = response.data

    if (emotionCycleData.value.error) {
      console.error('情绪周期数据加载失败:', emotionCycleData.value.error)
      return
    }

    await nextTick()
    setTimeout(() => renderEmotionCycleChart(true), 60)
  } catch (error) {
    console.error('加载情绪周期失败:', error)
  }
}

const intradayEmotionHint = computed(() => {
  const snapshot = emotionCycleData.value?.intraday_snapshot
  if (!snapshot?.is_temporary) return ''
  const asOf = String(snapshot.as_of || '').slice(0, 16)
  const total = Number(snapshot.total_stocks || 0).toLocaleString()
  return `\u76d8\u4e2d\u4e34\u65f6\u60c5\u7eea\uff1a${asOf}\uff0c${total}\u53ea\u80a1\u7968\uff0c\u57fa\u4e8eQMT 5\u5206\u949f\u884c\u60c5\uff1b\u6536\u76d8\u540e\u4ee5\u6b63\u5f0f\u65e5\u7ebf\u7ed3\u679c\u4e3a\u51c6\u3002`
})

const changeEmotionGranularity = async (granularity) => {
  if (emotionGranularity.value === granularity) return
  emotionGranularity.value = granularity
  clearEmotionHover()
  await loadEmotionCycle()
}


// 跳转到股指详情页
const goToIndexDetail = (code) => {
  router.push(`/index/${code}`)
}

const goToSystemConfigEmotion = () => {
  router.push('/system-config')
}

const toggleEmotionLegend = (name) => {
  emotionLegendSelected.value = {
    ...emotionLegendSelected.value,
    [name]: !emotionLegendSelected.value[name]
  }
  clearEmotionHover()
  nextTick(() => renderEmotionCycleChart(true))
}

const prevEmotionLegendPage = () => {
  if (emotionLegendPage.value <= 0) return
  emotionLegendPage.value -= 1
  clearEmotionHover()
}

const nextEmotionLegendPage = () => {
  if (emotionLegendPage.value >= emotionLegendPageCount.value - 1) return
  emotionLegendPage.value += 1
  clearEmotionHover()
}
const setEmotionHover = (index) => {
  hoveredEmotionIndex.value = index
}

const clearEmotionHover = () => {
  hoveredEmotionIndex.value = null
}

const selectSentimentPoint = (event) => {
  const days = emotionCurve30d.value.days
  if (!days.length) return
  const rect = event.currentTarget.getBoundingClientRect()
  const plotLeft = 44
  const plotRight = 18
  const usableWidth = Math.max(1, rect.width - plotLeft - plotRight)
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left - plotLeft) / usableWidth))
  const dataIndex = Math.round(ratio * (days.length - 1))
  selectedSentimentIndex.value = dataIndex
  sentimentCurveInstance.value?.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex })
}

const clearSelectedSentimentPoint = () => {
  selectedSentimentIndex.value = null
  sentimentCurveInstance.value?.dispatchAction({ type: 'hideTip' })
}

const renderSentimentCurveChart = () => {
  if (!sentimentCurveChart.value || !emotionCurve30d.value.days.length) return

  if (sentimentCurveInstance.value) {
    sentimentCurveInstance.value.dispose()
  }

  sentimentCurveInstance.value = echarts.init(sentimentCurveChart.value)

  const days = emotionCurve30d.value.days
  const raw = emotionCurve30d.value.raw || {}
  const normalized = emotionCurve30d.value.normalized || {}

  const seriesMeta = [
    { key: 'up_count_series', name: '上涨家数', color: '#ff6b6b' },
    { key: 'down_count_series', name: '下跌家数', color: '#28c76f' },
    { key: 'limit_up_count_series', name: '涨停家数', color: '#ff9f43' },
    { key: 'limit_down_count_series', name: '跌停家数', color: '#00cfe8' },
    { key: 'up_5_count_series', name: '涨幅>=5%', color: '#ea5455' },
    { key: 'down_5_count_series', name: '跌幅<=-5%', color: '#20c997' }
  ]

  const option = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      // 点击曲线后保留当天数据，便于逐日复盘；移动到其他日期时同步切换。
      triggerOn: 'mousemove|click',
      alwaysShowContent: true,
      backgroundColor: 'rgba(13, 18, 29, 0.94)',
      borderColor: '#26324d',
      textStyle: { color: '#eef3ff' },
      formatter: (params) => {
        const idx = params?.[0]?.dataIndex ?? 0
        const lines = [`<div style="margin-bottom:6px;font-weight:700;">${days[idx]}</div>`]
        params.forEach((item) => {
          const rawSeries = raw[item.seriesId] || []
          const rawVal = rawSeries[idx] ?? 0
          const delta = idx > 0 ? rawVal - (rawSeries[idx - 1] ?? 0) : 0
          const deltaText = idx > 0 ? ` (${delta >= 0 ? '+' : ''}${delta})` : ''
          lines.push(
            `<div style="display:flex;justify-content:space-between;gap:16px;">
              <span>${item.marker}${item.seriesName}</span>
              <span>${rawVal}${deltaText}</span>
            </div>`
          )
        })
        return lines.join('')
      }
    },
    legend: {
      type: 'scroll',
      top: 0,
      left: 0,
      right: 8,
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: '#627191', fontSize: 11 }
    },
    grid: {
      left: 30,
      right: 14,
      top: 34,
      bottom: 26,
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: days,
      axisLine: { lineStyle: { color: '#d7def2' } },
      axisLabel: {
        color: '#7a89ab',
        fontSize: 10,
        interval: Math.max(0, Math.floor(days.length / 6) - 1),
        formatter: (value) => formatTrendDate(value)
      }
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: '#8a97b8',
        fontSize: 10,
        formatter: '{value}'
      },
      splitLine: {
        lineStyle: {
          color: 'rgba(140, 155, 187, 0.18)',
          type: 'dashed'
        }
      }
    },
    series: seriesMeta.map((meta) => ({
      id: meta.key,
      name: meta.name,
      type: 'line',
      smooth: true,
      showSymbol: false,
      symbolSize: 5,
      data: normalized[meta.key] || [],
      lineStyle: {
        width: meta.key.includes('limit_') ? 1.8 : 2.3,
        color: meta.color
      },
      itemStyle: { color: meta.color },
      emphasis: { focus: 'series' }
    }))
  }

  sentimentCurveInstance.value.setOption(option)
}

// 渲染情绪周期图表
const handleTurnoverHover = (event) => {
  const days = turnoverTrend.value.days
  if (!days.length) return
  const rect = event.currentTarget.getBoundingClientRect()
  const plotLeft = 56
  const plotRight = 18
  const usableWidth = Math.max(1, rect.width - plotLeft - plotRight)
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left - plotLeft) / usableWidth))
  hoveredTurnoverIndex.value = Math.round(ratio * (days.length - 1))

  const tooltipWidth = 230
  const tooltipHeight = 170
  const pointerX = event.clientX - rect.left
  const pointerY = event.clientY - rect.top
  turnoverTooltipPosition.value = {
    x: Math.max(8, Math.min(rect.width - tooltipWidth - 8, pointerX + 14)),
    y: pointerY > rect.height * 0.56 ? Math.max(8, pointerY - tooltipHeight) : Math.min(rect.height - tooltipHeight - 8, pointerY + 14)
  }
}

const clearTurnoverHover = () => {
  hoveredTurnoverIndex.value = null
}

const renderTurnoverTrendChart = () => {
  if (!turnoverTrendChart.value || !turnoverTrend.value.days.length) return
  if (turnoverTrendInstance.value) turnoverTrendInstance.value.dispose()

  turnoverTrendInstance.value = echarts.init(turnoverTrendChart.value)
  const { days, amounts, shAmounts, szAmounts, bjAmounts, threshold } = turnoverTrend.value
  const shAmountsInBillion = shAmounts.map((value) => value / 100_000_000)
  const szAmountsInBillion = szAmounts.map((value) => value / 100_000_000)
  const bjAmountsInBillion = bjAmounts.map((value) => value / 100_000_000)
  const thresholdInBillion = threshold / 100_000_000
  const forecast = intradayTurnoverForecast.value
  const forecastAmountInBillion = forecast.available ? Number(forecast.forecast_amount || 0) / 100_000_000 : 0
  const forecastRemainingInBillion = amounts.map((amount, index) => (
    index === amounts.length - 1 && forecastAmountInBillion > amount / 100_000_000
      ? forecastAmountInBillion - amount / 100_000_000
      : 0
  ))
  const forecastMarkPoint = forecastRemainingInBillion.some((value) => value > 0)
    ? [{
        name: '预计全天',
        coord: [days[days.length - 1], forecastAmountInBillion],
        value: forecastAmountInBillion,
        itemStyle: { color: '#facc15' }
      }]
    : []
  const turnoverExtrema = amounts
    .map((amount, index) => ({ index, value: Number(amount) / 100_000_000 }))
    .filter((item) => Number.isFinite(item.value))
  if (!turnoverExtrema.length) return
  const minTurnover = turnoverExtrema.reduce((min, item) => item.value < min.value ? item : min, turnoverExtrema[0])
  const maxTurnover = turnoverExtrema.reduce((max, item) => item.value > max.value ? item : max, turnoverExtrema[0])
  const turnoverExtremaMarkPoints = [
    { name: '最低值', coord: [days[minTurnover.index], minTurnover.value], value: minTurnover.value, itemStyle: { color: '#60a5fa' } },
    { name: '最高值', coord: [days[maxTurnover.index], maxTurnover.value], value: maxTurnover.value, itemStyle: { color: '#fbbf24' } }
  ]
  const changeDirections = amounts.map((amount, index) => {
    if (index === 0) return 'flat'
    return amount > amounts[index - 1] ? 'up' : (amount < amounts[index - 1] ? 'down' : 'flat')
  })
  const segmentColors = {
    up: ['#ef4444', '#f97316', '#fca5a5'],
    down: ['#16a34a', '#22c55e', '#86efac'],
    flat: ['#64748b', '#94a3b8', '#cbd5e1']
  }
  const segmentColor = (index, segmentIndex) => segmentColors[changeDirections[index]][segmentIndex]
  turnoverTrendInstance.value.setOption({
    animation: false,
    tooltip: {
      trigger: 'axis', backgroundColor: 'rgba(13, 18, 29, 0.94)', borderColor: '#26324d', textStyle: { color: '#eef3ff' },
      formatter: (params) => {
        const item = params?.[0]
        if (!item) return ''
        const index = item.dataIndex
        const value = Number(amounts[index] || 0) / 100_000_000
        const active = value >= thresholdInBillion
        const previous = index > 0 ? Number(amounts[index - 1] || 0) : null
        const change = previous === null ? 0 : Number(amounts[index] || 0) - previous
        const changePercent = previous ? change / previous * 100 : 0
        const changeText = previous === null
          ? '首个统计日，无前日对比'
          : `较前日${change > 0 ? '放量' : (change < 0 ? '缩量' : '持平')} ${(Math.abs(change) / 1000000000000).toFixed(2)} 万亿（${change > 0 ? '+' : ''}${changePercent.toFixed(2)}%）`
        const changeColor = change > 0 ? '#f87171' : (change < 0 ? '#4ade80' : '#cbd5e1')
        const markets = [
          { label: '沪市', value: shAmounts[index], color: segmentColor(index, 0) },
          { label: '深市', value: szAmounts[index], color: segmentColor(index, 1) },
          { label: '北交所', value: bjAmounts[index], color: segmentColor(index, 2) }
        ]
        const marketLines = markets.map((market) => `
          <div style="display:flex;justify-content:space-between;gap:26px;margin-top:4px;">
            <span><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${market.color};margin-right:6px;"></i>${market.label}</span>
            <b>${(Number(market.value || 0) / 10000).toFixed(2)} 万亿</b>
          </div>`).join('')
        return `<div style="font-weight:700;margin-bottom:6px;">${formatTrendDate(days[index])}</div><div style="display:flex;justify-content:space-between;gap:26px;border-bottom:1px solid #334155;padding-bottom:6px;"><span>三市合计</span><b>${(value / 10000).toFixed(2)} 万亿</b></div>${marketLines}<div style="margin-top:7px;color:${changeColor};">${changeText}</div><div style="margin-top:4px;color:${active ? '#86efac' : '#fda4af'};">${active ? '交易活跃，可交易环境' : '低于 2 万亿：交易冷清，赚钱难度极大'}</div>`
      }
    },
    grid: { left: 34, right: 18, top: 48, bottom: 30, containLabel: true },
    xAxis: { type: 'category', data: days, axisLine: { lineStyle: { color: '#d7def2' } }, axisLabel: { color: '#8a97b8', fontSize: 10, interval: Math.max(0, Math.floor(days.length / 6) - 1), formatter: formatTrendDate } },
    yAxis: { type: 'value', axisLabel: { color: '#8a97b8', fontSize: 10, formatter: (value) => `${(value / 10000).toFixed(1)}万亿` }, splitLine: { lineStyle: { color: 'rgba(140, 155, 187, 0.18)', type: 'dashed' } } },
    series: [
      {
        name: '沪市', type: 'bar', stack: '三市成交额', barMaxWidth: 24, data: shAmountsInBillion,
        itemStyle: { color: (item) => segmentColor(item.dataIndex, 0), borderRadius: [0, 0, 0, 0] }
      },
      {
        name: '深市', type: 'bar', stack: '三市成交额', barMaxWidth: 24, data: szAmountsInBillion,
        itemStyle: { color: (item) => segmentColor(item.dataIndex, 1), borderRadius: [0, 0, 0, 0] }
      },
      {
        name: '北交所', type: 'bar', stack: '三市成交额', barMaxWidth: 24, data: bjAmountsInBillion,
        itemStyle: { color: (item) => segmentColor(item.dataIndex, 2), borderRadius: [4, 4, 0, 0] },
        markLine: { symbol: 'none', lineStyle: { color: '#facc15', type: 'dashed', width: 2 }, label: { color: '#fef3c7', formatter: '2万亿交易活跃底线' }, data: [{ yAxis: thresholdInBillion }] },
        markPoint: {
          symbol: 'circle',
          symbolSize: 7,
          label: {
            show: true,
            position: 'top',
            distance: 7,
            color: '#f8fafc',
            fontSize: 11,
            fontWeight: 700,
            backgroundColor: 'rgba(15, 23, 42, 0.88)',
            borderRadius: 3,
            padding: [3, 5],
            formatter: ({ name, value }) => `${name} ${(Number(value) / 10000).toFixed(2)}万亿`
          },
          data: turnoverExtremaMarkPoints
        }
      },
      {
        name: '预计剩余成交额（虚拟）', type: 'bar', stack: '三市成交额', barMaxWidth: 24, data: forecastRemainingInBillion,
        itemStyle: { color: 'rgba(250, 204, 21, 0.26)', borderColor: '#facc15', borderType: 'dashed', borderWidth: 1, borderRadius: [4, 4, 0, 0] },
        markPoint: {
          symbol: 'diamond',
          symbolSize: 9,
          label: {
            show: true,
            position: 'top',
            distance: 7,
            color: '#fef3c7',
            fontSize: 11,
            fontWeight: 700,
            backgroundColor: 'rgba(15, 23, 42, 0.9)',
            borderRadius: 3,
            padding: [3, 5],
            formatter: ({ value }) => `预计全天 ${(Number(value) / 10000).toFixed(2)}万亿`
          },
          data: forecastMarkPoint
        }
      }
    ]
  })
}

/* const renderEmotionCycleChart = () => {
  if (!emotionCycleChart.value || !emotionCycleData.value || emotionCycleData.value.error) return

  if (emotionCycleInstance.value) {
    emotionCycleInstance.value.dispose()
  }

  emotionCycleInstance.value = echarts.init(emotionCycleChart.value)

  const ec = emotionCycleData.value

  // 根据上涨率值获取颜色（深红色=高，深绿色=低，中间渐变）
  const getColorByRate = (rate) => {
    if (rate === null || rate === undefined) return '#9ca3af'
    const normalizedRate = Math.max(0, Math.min(100, rate))
    const ratio = normalizedRate / 100

    // 深绿色 (low) -> 黄色 (mid) -> 深红色 (high)
    // Low: #006400 (深绿)
    // Mid: #FFD700 (金黄色)
    // High: #8B0000 (深红)
    if (ratio < 0.5) {
      // 绿色到黄色的渐变
      const r = Math.round(0 + (255 - 0) * (ratio * 2))
      const g = Math.round(100 + (215 - 100) * (ratio * 2))
      const b = 0
      return `rgb(${r}, ${g}, ${b})`
    } else {
      // 黄色到红色的渐变
      const r = Math.round(255 + (139 - 255) * ((ratio - 0.5) * 2))
      const g = Math.round(215 + (0 - 215) * ((ratio - 0.5) * 2))
      const b = 0
      return `rgb(${r}, ${g}, ${b})`
    }
  }

  // 计算每条线的平均颜色用于图例
  const getSeriesAvgColor = (data) => {
    if (!data || data.length === 0) return '#9ca3af'
    const validValues = data.filter(v => v !== null && v !== undefined)
    if (validValues.length === 0) return '#9ca3af'
    const avg = validValues.reduce((sum, v) => sum + v, 0) / validValues.length
    return getColorByRate(avg)
  }

  const option = {
    visualMap: {
      min: 0,
      max: 100,
      text: ['高', '低'],
      realtime: false,
      calculable: true,
      inRange: {
        color: [
          '#006400',  // 深绿色 (0%)
          '#32CD32',  // 橄榄绿 (20%)
          '#FFD700',  // 金黄色 (50%)
          '#FF6347',  // 橙红色 (75%)
          '#8B0000'   // 深红色 (100%)
        ]
      },
      show: false  // 隐藏visualMap组件
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } },
      backgroundColor: 'rgba(0, 0, 0, 0.9)',
      borderColor: '#333',
      borderWidth: 1,
      borderRadius: 4,
      shadowBlur: 10,
      shadowColor: 'rgba(0, 0, 0, 0.5)',
      padding: [12, 16],
      textStyle: { fontSize: 12, color: '#fff' },
      formatter: function(params) {
        if (!params || !Array.isArray(params) || params.length === 0) return ''

        const date = params[0].axisValue
        let result = `<div style="font-weight:600;margin-bottom:8px;color:#fff;font-size:13px;">${date}</div>`

        params.forEach(function(param) {
          if (!param) return

          const value = param.value
          const seriesName = param.seriesName || ''
          const itemColor = param.color || '#9ca3af'

          // 显示所有指标，包括 null 值显示为 '--'
          if (value !== null && value !== undefined && typeof value === 'number') {
            const valueStr = value.toFixed(2)

            result += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
              <span style="width:8px;height:8px;border-radius:50%;background:${itemColor};flex-shrink:0;"></span>
              <span style="color:#ddd;font-size:12px;flex:1;">${seriesName}</span>
              <span style="font-weight:700;color:#fff;font-size:12px;">${valueStr}</span>
            </div>`
          }
        })
        return result
      }
    },
    legend: {
      data: ['收盘上涨率', '盘中上涨率', '上证上涨率', '深圳上涨率', '创业板上涨率', '昨日妖股上涨率', '昨日强势股上涨率', '昨日弱势股上涨率'],
      top: 10,
      left: 'center',
      textStyle: { fontSize: 12, color: '#4a5568' },
      itemGap: 15,
      type: 'scroll'
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '5%',
      top: '80px',
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: ec.dates || [],
      axisLine: { lineStyle: { color: '#cbd5e0' } },
      axisLabel: {
        fontSize: 11,
        color: '#718096',
        rotate: 30,
        formatter: (value) => {
          if (value.includes('-')) {
            const parts = value.split('-')
            return `${parts[1]}-${parts[2]}`
          }
          return value
        }
      }
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      name: '上涨率(%)',
      nameTextStyle: { padding: [0, 0, 0, 20], color: '#718096' },
      axisLabel: {
        formatter: '{value}%',
        fontSize: 11,
        color: '#718096'
      },
      splitLine: {
        lineStyle: {
          type: 'dashed',
          color: '#e2e8f0'
        }
      }
    },
    series: [
      {
        name: '收盘上涨率',
        type: 'line',
        data: ec.close_up_rate || [],
        smooth: true,
        lineStyle: { width: 2.5 },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '盘中上涨率',
        type: 'line',
        data: ec.intraday_up_rate || [],
        smooth: true,
        lineStyle: { width: 2, type: 'dashed' },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '上证上涨率',
        type: 'line',
        data: ec.sh_up_rate || [],
        smooth: true,
        lineStyle: { width: 2 },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '深圳上涨率',
        type: 'line',
        data: ec.sz_up_rate || [],
        smooth: true,
        lineStyle: { width: 2 },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '创业板上涨率',
        type: 'line',
        data: ec.cyb_up_rate || [],
        smooth: true,
        lineStyle: { width: 2 },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '昨日妖股上涨率',
        type: 'line',
        data: ec.yesterday_monster_up_rate || [],
        smooth: true,
        lineStyle: { width: 2, type: 'dashdot' },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '昨日强势股上涨率',
        type: 'line',
        data: ec.yesterday_strong_up_rate || [],
        smooth: true,
        lineStyle: { width: 2 },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      },
      {
        name: '昨日弱势股上涨率',
        type: 'line',
        data: ec.yesterday_weak_up_rate || [],
        smooth: true,
        lineStyle: { width: 2, type: 'dotted' },
        showSymbol: true,
        symbol: 'circle',
        symbolSize: 6,
        emphasis: { focus: 'none', scale: true }
      }
    ]
  }

  emotionCycleInstance.value.setOption(option)
}

const renderEmotionCycleChartReference = (forceReinit = false, retryCount = 0) => {
  if (!emotionCycleChart.value || !emotionCycleData.value || emotionCycleData.value.error) return

  const rect = emotionCycleChart.value.getBoundingClientRect()
  if ((rect.width < 120 || rect.height < 160) && retryCount < 8) {
    setTimeout(() => renderEmotionCycleChartReference(forceReinit, retryCount + 1), 60)
    return
  }

  if (forceReinit) {
    const existingInstance = echarts.getInstanceByDom(emotionCycleChart.value)
    if (existingInstance) {
      existingInstance.dispose()
    }
    emotionCycleInstance.value = null
  }

  if (!emotionCycleInstance.value) {
    emotionCycleInstance.value = echarts.init(emotionCycleChart.value)
  }

  const ec = emotionCycleData.value
  const seriesMeta = activeEmotionSeriesMeta.value
  if (seriesMeta.length === 0) {
    emotionCycleInstance.value.setOption({ series: [] }, { notMerge: true, lazyUpdate: true })
    return
  }
  const legendNames = seriesMeta.map((item) => item.name)
  const xAxisDates = ec.dates || []
  const seriesDataMap = Object.fromEntries(seriesMeta.map((item) => [item.name, ec[item.key] || []]))

  const option = {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      confine: true,
      padding: [10, 12],
      backgroundColor: 'rgba(17, 24, 39, 0.94)',
      borderColor: 'rgba(148, 163, 184, 0.25)',
      borderWidth: 1,
      borderRadius: 8,
      textStyle: {
        color: '#f8fafc',
        fontSize: 12
      },
      axisPointer: {
        type: 'line',
        snap: true,
        lineStyle: {
          color: 'rgba(148, 163, 184, 0.7)',
          width: 1,
          type: 'dashed'
        },
        label: { show: false }
      },
      formatter: (params) => {
        if (!Array.isArray(params) || params.length === 0) return ''

        const rows = [...params].sort((a, b) => legendNames.indexOf(a.seriesName) - legendNames.indexOf(b.seriesName))
        const title = `<div style="font-weight:700;margin-bottom:8px;color:#f8fafc;font-size:13px;">${formatTrendDate(params[0].axisValue)}</div>`
        const lines = rows.map((param) => {
          const seriesName = param.seriesName || '--'
          const itemColor = typeof param.color === 'string' ? param.color : '#9ca3af'
          const seriesValues = seriesDataMap[seriesName] || []
          const rawValue = seriesValues[param.dataIndex]
          const value = typeof rawValue === 'number' ? `${rawValue.toFixed(2)}%` : '--'

          return `<div style="display:grid;grid-template-columns:10px minmax(96px,1fr) auto;align-items:center;column-gap:8px;margin-bottom:6px;">
            <span style="width:8px;height:8px;border-radius:50%;background:${itemColor};display:block;"></span>
            <span style="color:#cbd5e1;font-size:12px;white-space:nowrap;">${seriesName}</span>
            <span style="font-weight:700;color:#f8fafc;font-size:12px;">${value}</span>
          </div>`
        })
        return title + lines.join('')
      }
    },
    grid: {
      left: 44,
      right: 24,
      top: 28,
      bottom: 26,
      containLabel: true
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: xAxisDates,
      axisLine: {
        lineStyle: { color: 'rgba(255, 255, 255, 0.16)' }
      },
      axisTick: { show: false },
      axisLabel: {
        color: '#cbd5e1',
        fontSize: 11,
        rotate: 0,
        margin: 10,
        formatter: (value) => formatTrendDate(value)
      }
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      name: '上涨率(%)',
      nameTextStyle: {
        color: '#cbd5e1',
        fontSize: 12,
        padding: [0, 0, 8, 4]
      },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: '#cbd5e1',
        fontSize: 11,
        formatter: '{value}%'
      },
      splitLine: {
        lineStyle: {
          type: 'solid',
          color: 'rgba(255, 255, 255, 0.10)'
        }
      }
    },
    series: seriesMeta.map((meta) => ({
      name: meta.name,
      id: meta.key,
      type: 'line',
      data: seriesDataMap[meta.name] || [],
      smooth: false,
      showSymbol: false,
      showAllSymbol: false,
      symbol: 'circle',
      symbolSize: 4,
      lineStyle: {
        color: meta.color,
        width: meta.width,
        ...(meta.lineType !== 'solid' ? { type: meta.lineType } : {})
      },
      itemStyle: { color: meta.color }
    }))
  }

  try {
    emotionCycleInstance.value.setOption(option, { notMerge: true, lazyUpdate: true, silent: true })
  } catch (error) {
    if (!forceReinit && retryCount < 2) {
      setTimeout(() => renderEmotionCycleChartReference(true, retryCount + 1), 0)
      return
    }
    console.warn('情绪周期图表渲染失败:', error)
  }
}

// 格式化价格
*/
const renderEmotionCycleChart = (forceReinit = false, retryCount = 0) => {
  if (!emotionCycleChart.value || !emotionCycleData.value || emotionCycleData.value.error) return
  const rect = emotionCycleChart.value.getBoundingClientRect()
  if ((rect.width < 120 || rect.height < 180) && retryCount < 8) {
    setTimeout(() => renderEmotionCycleChart(forceReinit, retryCount + 1), 60)
    return
  }
  if (forceReinit) {
    const existingInstance = echarts.getInstanceByDom(emotionCycleChart.value)
    if (existingInstance) existingInstance.dispose()
    emotionCycleInstance.value = null
  }
  if (!emotionCycleInstance.value) {
    emotionCycleInstance.value = echarts.init(emotionCycleChart.value)
  }
  const ec = emotionCycleData.value
  const dates = ec.dates || []
  const seriesMeta = activeEmotionSeriesMeta.value
  const pieces = emotionScoreBands.map((band, index) => ({
    min: band.min,
    max: index === emotionScoreBands.length - 1 ? 101 : band.max,
    color: band.color
  }))
  const series = seriesMeta.map((meta, index) => ({
    id: meta.key,
    name: meta.name,
    type: 'line',
    smooth: 0.42,
    showSymbol: true,
    showAllSymbol: false,
    symbol: 'circle',
    symbolSize: 4,
    data: dates.map((_, dataIndex) => Number(ec[meta.key]?.[dataIndex] ?? 0)),
    lineStyle: {
      width: index < 2 ? 2.2 : 1.7,
      opacity: 0.95,
      color: meta.color,
      type: meta.lineType === 'dashed' ? 'dashed' : 'solid'
    },
    itemStyle: { color: meta.color, borderColor: '#f8fafc', borderWidth: 1 },
    emphasis: { focus: 'series', lineStyle: { width: 3 } }
  }))
  const option = {
    backgroundColor: 'transparent',
    animation: false,
    color: seriesMeta.map((meta) => meta.color),
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: 'rgba(245, 247, 251, 0.98)',
      borderColor: 'rgba(15, 23, 42, 0.12)',
      borderRadius: 6,
      padding: [10, 12],
      textStyle: { color: '#1f2937', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(226, 232, 240, 0.72)', width: 1, type: 'dashed' } },
      formatter: (params) => {
        if (!Array.isArray(params) || params.length === 0) return ''
        const rows = [...params].sort((a, b) => seriesMeta.findIndex((item) => item.name === a.seriesName) - seriesMeta.findIndex((item) => item.name === b.seriesName))
        const title = `<div style="font-weight:800;margin-bottom:8px;font-size:13px;color:#111827;">${formatTrendDateCompact(params[0].axisValue)}</div>`
        const lines = rows.map((param) => {
          const value = Number(param.value || 0)
          const band = getEmotionScoreBand(value)
          return `<div style="display:grid;grid-template-columns:10px minmax(120px,1fr) auto;align-items:center;column-gap:8px;margin-bottom:6px;">`
            + `<span style="width:8px;height:8px;border-radius:50%;background:${band.color};display:block;"></span>`
            + `<span style="color:#4b5563;white-space:nowrap;">${param.seriesName}</span>`
            + `<span style="font-weight:800;color:#111827;">${value.toFixed(2)}</span></div>`
        })
        return title + lines.join('')
      }
    },
    grid: { left: 44, right: 32, top: 22, bottom: 34, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLine: { lineStyle: { color: 'rgba(203, 213, 225, 0.28)' } },
      axisTick: { show: false },
      axisLabel: { color: '#cbd5e1', fontSize: 11, margin: 12, formatter: (value) => formatTrendDate(value) }
    },
    yAxis: {
      type: 'value',
      min: 0,
      max: 100,
      interval: 20,
      name: '上涨比例(%)',
      nameTextStyle: { color: '#e5e7eb', fontSize: 12, fontWeight: 700, padding: [0, 0, 8, 2] },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#cbd5e1', fontSize: 11, formatter: '{value}%' },
      splitLine: { lineStyle: { color: 'rgba(255, 255, 255, 0.12)', type: 'solid' } },
      splitArea: {
        show: true,
        areaStyle: {
          color: emotionScoreBands.map((band) => band.bg)
        }
      }
    },
    series
  }
  emotionCycleInstance.value.setOption(option, { notMerge: true, lazyUpdate: true })
}

const formatPrice = (price) => {
  if (!price) return '--'
  return price.toFixed(2)
}

// 格式化金额
const formatAmount = (amount) => {
  if (!amount) return '--'
  if (amount >= 100000000) {
    return (amount / 100000000).toFixed(2) + '亿'
  } else if (amount >= 10000) {
    return (amount / 10000).toFixed(2) + '万'
  }
  return amount.toFixed(2)
}

// 计算上涨宽度
const getUpWidth = (sentiment) => {
  const total = (sentiment.up_count || 0) + (sentiment.down_count || 0) + (sentiment.unchanged_count || 0)
  if (total === 0) return '50%'
  return ((sentiment.up_count || 0) / total * 100) + '%'
}

const getDistributionHeight = (count) => {
  const list = sentimentDistribution.value
  const maxCount = Math.max(...list.map(item => item.count), 1)
  const ratio = Number(count || 0) / maxCount
  return `${Math.max(6, Math.round(ratio * 120))}px`
}

const formatTrendDate = (dateStr) => {
  if (!dateStr || typeof dateStr !== 'string') return '--'
  if (dateStr.includes(' ')) {
    const [datePart, timePart = ''] = dateStr.split(' ')
    const parts = datePart.split('-')
    const hour = timePart.slice(0, 5)
    if (parts.length === 3 && hour) return `${parts[1]}-${parts[2]} ${hour}`
  }
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  return `${parts[1]}-${parts[2]}`
}

const formatTrendDateCompact = (dateStr) => {
  if (!dateStr || typeof dateStr !== 'string') return '--'
  if (dateStr.includes(' ')) {
    const [datePart, timePart = ''] = dateStr.split(' ')
    const parts = datePart.split('-')
    const hour = timePart.slice(0, 5)
    if (parts.length === 3 && hour) return `${parts[0]}${parts[1]}${parts[2]} ${hour}`
  }
  const parts = dateStr.split('-')
  if (parts.length !== 3) return dateStr
  return `${parts[0]}${parts[1]}${parts[2]}`
}

// 获取评分样式类
const getScoreClass = (score) => {
  if (score >= 70) return 'high'
  if (score >= 40) return 'medium'
  return 'low'
}

// 窗口大小变化处理
const handleResize = () => {
  try {
    if (turnoverTrendInstance.value) {
      turnoverTrendInstance.value.resize()
    }
    if (sentimentCurveInstance.value) {
      sentimentCurveInstance.value.resize()
    }
    if (emotionCycleInstance.value) {
      emotionCycleInstance.value.resize()
    }
  } catch (e) {
    console.warn('图表resize失败:', e)
  }
}

onMounted(() => {
  loadMarketData()
  marketRefreshTimer = window.setInterval(() => {
    loadMarketData()
  }, 60 * 1000)
  emotionRefreshTimer = window.setInterval(() => {
    loadEmotionCycle()
  }, 60 * 1000)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  if (marketRefreshTimer) {
    window.clearInterval(marketRefreshTimer)
    marketRefreshTimer = null
  }
  if (emotionRefreshTimer) {
    window.clearInterval(emotionRefreshTimer)
    emotionRefreshTimer = null
  }
  if (marketDataRetryTimer) {
    window.clearTimeout(marketDataRetryTimer)
    marketDataRetryTimer = null
  }
  window.removeEventListener('resize', handleResize)
  if (turnoverTrendInstance.value) {
    turnoverTrendInstance.value.dispose()
    turnoverTrendInstance.value = null
  }
  if (emotionCycleInstance.value) {
    try {
      emotionCycleInstance.value.dispose()
      emotionCycleInstance.value = null
    } catch (e) {
      console.warn('销毁图表实例失败:', e)
    }
  }
  if (sentimentCurveInstance.value) {
    try {
      sentimentCurveInstance.value.dispose()
      sentimentCurveInstance.value = null
    } catch (e) {
      console.warn('销毁情绪曲线实例失败:', e)
    }
  }
})
</script>

<style scoped>
.home {
  min-height: calc(100vh - 60px);
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 20px;
}

.container {
  max-width: 1400px;
  margin: 0 auto;
}

.header {
  text-align: center;
  color: white;
  margin-bottom: 30px;
}

.header h1 {
  font-size: 36px;
  margin-bottom: 8px;
}

.header .date {
  font-size: 16px;
  opacity: 0.9;
}

.header-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
}

.no-data-card {
  background: white;
  border-radius: 12px;
  padding: 40px;
  text-align: center;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

.no-data-icon {
  font-size: 48px;
  margin-bottom: 16px;
}

.no-data-text {
  font-size: 18px;
  color: #718096;
  margin-bottom: 8px;
}

.no-data-hint {
  font-size: 14px;
  color: #a0aec0;
}

.section-title {
  color: white;
  font-size: 20px;
  margin-bottom: 16px;
  padding-left: 12px;
  border-left: 4px solid rgba(255, 255, 255, 0.5);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.more-btn {
  padding: 6px 12px;
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 6px;
  color: white;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.3s;
}

.more-btn:hover {
  background: rgba(255, 255, 255, 0.3);
}

.more-btn.active {
  background: rgba(255, 255, 255, 0.4);
  font-weight: 500;
}

.calculate-btn {
  padding: 8px 16px;
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 6px;
  color: white;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.3s;
}

.calculate-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.3);
}

.calculate-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 指数卡片 */
.indices-section {
  margin-bottom: 30px;
}

.indices-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}

.index-card {
  background: white;
  border-radius: 12px;
  padding: 20px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

.index-card.up {
  border-left: 4px solid #f44336;
}

.index-card.down {
  border-left: 4px solid #4caf50;
}

.index-name {
  font-size: 14px;
  color: #718096;
  margin-bottom: 4px;
}

.index-code {
  font-size: 12px;
  color: #a0aec0;
  margin-bottom: 8px;
}

.index-price {
  font-size: 28px;
  font-weight: bold;
  color: #2d3748;
  margin-bottom: 8px;
}

.index-card.up .index-price {
  color: #f44336;
}

.index-card.down .index-price {
  color: #4caf50;
}

.index-change {
  margin-bottom: 8px;
}

.index-change .change-value {
  font-size: 18px;
  font-weight: 600;
  margin-right: 12px;
}

.index-card.up .change-value {
  color: #f44336;
}

.index-card.down .change-value {
  color: #4caf50;
}

.index-change .change-amount {
  font-size: 14px;
  color: #718096;
}

.index-amount {
  font-size: 13px;
  color: #a0aec0;
}

/* 涨跌统计 */
.sentiment-section {
  margin-bottom: 30px;
}

.sentiment-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
}

.stat-card {
  background: white;
  border-radius: 12px;
  padding: 20px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

.stat-title {
  font-size: 14px;
  color: #718096;
  margin-bottom: 16px;
  font-weight: 500;
}

.stat-content.up-down {
  display: flex;
  justify-content: space-around;
  text-align: center;
}

.up-count .count {
  display: block;
  font-size: 28px;
  font-weight: bold;
  color: #f44336;
}

.down-count .count {
  display: block;
  font-size: 28px;
  font-weight: bold;
  color: #4caf50;
}

.unchanged-count .count {
  display: block;
  font-size: 28px;
  font-weight: bold;
  color: #9e9e9e;
}

.up-count .label,
.down-count .label,
.unchanged-count .label {
  font-size: 12px;
  color: #a0aec0;
}

.up-count.limit .count {
  color: #d32f2f;
  text-shadow: 0 0 8px rgba(211, 47, 47, 0.3);
}

.down-count.limit .count {
  color: #388e3c;
  text-shadow: 0 0 8px rgba(56, 142, 60, 0.3);
}

.stat-bar {
  height: 6px;
  background: #4caf50;
  border-radius: 3px;
  margin-top: 16px;
  overflow: hidden;
}

.bar-up {
  height: 100%;
  background: #f44336;
  border-radius: 3px;
  transition: width 0.3s;
}

/* 两市成交 */
.stat-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.amount-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid #f1f1f1;
}

.amount-item:last-child {
  border-bottom: none;
}

.amount-item.total {
  border-top: 2px solid #e2e8f0;
  padding-top: 12px;
  margin-top: 4px;
}

.amount-item .label {
  font-size: 14px;
  color: #718096;
}

.amount-item .amount {
  font-size: 18px;
  font-weight: 600;
  color: #2d3748;
}

.amount-item.total .amount {
  font-size: 20px;
  font-weight: bold;
  color: #667eea;
}

.sentiment-panel-row {
  margin-top: 16px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}

.emotion-curve-card {
  background: #ffffff;
  border-radius: 12px;
  padding: 14px 16px 10px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
  min-height: 286px;
}

.curve-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.curve-card-title {
  margin: 0;
  font-size: 15px;
  color: #334266;
}

.curve-card-subtitle {
  font-size: 11px;
  color: #7c8aac;
  margin-bottom: 8px;
}

.curve-card-latest {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.curve-snapshot {
  border-radius: 999px;
  padding: 3px 8px;
  font-size: 12px;
  font-weight: 700;
}

.curve-snapshot.up {
  background: rgba(239, 68, 68, 0.1);
  color: #e24747;
}

.curve-snapshot.down {
  background: rgba(22, 163, 74, 0.1);
  color: #14814f;
}

.curve-snapshot.limit-up {
  background: rgba(255, 159, 67, 0.14);
  color: #d97706;
}

.curve-snapshot.limit-down {
  background: rgba(0, 207, 232, 0.14);
  color: #0f8ea0;
}

.emotion-curve-chart {
  width: 100%;
  min-height: 220px;
  cursor: crosshair;
}

.sentiment-click-details {
  margin: 4px 0 2px;
  padding: 9px 10px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
  color: #334155;
  font-size: 12px;
}

.sentiment-click-details-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 7px;
}

.sentiment-click-details-header strong { color: #1e3a8a; }
.sentiment-click-details-header span { color: #64748b; }
.sentiment-click-details-header button {
  margin-left: auto;
  padding: 2px 7px;
  border: 1px solid #cbd5e1;
  border-radius: 5px;
  background: #fff;
  color: #475569;
  cursor: pointer;
}

.sentiment-click-details-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 5px 10px;
}

.distribution-wrapper {
  margin-top: 0;
}

.distribution-card {
  background: #12151c;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
}

.distribution-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.distribution-title {
  margin: 0;
  font-size: 16px;
  color: #f0f3ff;
}

.distribution-summary {
  font-size: 16px;
  font-weight: 700;
  display: flex;
  align-items: center;
  gap: 6px;
}

.distribution-summary .sum-up {
  color: #f56c6c;
}

.distribution-summary .sum-flat {
  color: #f6d26b;
}

.distribution-summary .sum-down {
  color: #45c58a;
}

.distribution-summary .sep {
  color: #9fb0d8;
}

.distribution-bars {
  display: grid;
  grid-template-columns: repeat(9, minmax(40px, 1fr));
  align-items: end;
  gap: 10px;
  min-height: 160px;
}

.dist-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: end;
  gap: 4px;
}

.dist-bar {
  width: 100%;
  max-width: 32px;
  border-radius: 6px 6px 0 0;
  transition: all 0.3s ease;
}

.dist-up {
  background: linear-gradient(180deg, #ff7a7a 0%, #e24747 100%);
}

.dist-flat {
  background: linear-gradient(180deg, #f6dc7d 0%, #cdaa3f 100%);
}

.dist-down {
  background: linear-gradient(180deg, #53d29a 0%, #1d9f68 100%);
}

.dist-label {
  font-size: 12px;
  color: #b8c4e5;
}

.dist-count {
  font-size: 12px;
  color: #ecf0ff;
  font-weight: 600;
}





.chart-actions-hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
}

/* 情绪周期 */
.turnover-trend-section {
  margin: 0 0 30px;
  padding: 16px;
  border-radius: 14px;
  background: linear-gradient(180deg, #20252f 0%, #171c26 100%);
  border: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow: 0 20px 40px rgba(10, 14, 24, 0.28);
}

.turnover-trend-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 10px;
}

.turnover-trend-header p {
  margin: 6px 0 0;
  color: #b8c4e5;
  font-size: 13px;
}

.turnover-trend-status {
  white-space: nowrap;
  padding: 7px 10px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 700;
}

.turnover-trend-status.active { background: rgba(22, 163, 74, 0.22); color: #86efac; }
.turnover-trend-status.cold { background: rgba(220, 38, 38, 0.22); color: #fda4af; }
.turnover-forecast-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin: 0 0 10px;
  padding: 8px 10px;
  border: 1px solid rgba(96, 165, 250, 0.28);
  border-radius: 8px;
  background: rgba(30, 64, 175, 0.12);
  color: #bfdbfe;
  font-size: 12px;
}
.turnover-forecast-row strong { color: #fef08a; }
.turnover-forecast-row.unavailable { border-color: rgba(148, 163, 184, 0.22); background: rgba(71, 85, 105, 0.14); color: #cbd5e1; }
.turnover-trend-chart-wrap { position: relative; height: 285px; width: 100%; }
.turnover-trend-chart { height: 100%; width: 100%; }
.turnover-chart-tooltip {
  position: absolute;
  z-index: 20;
  min-width: 210px;
  padding: 10px 12px;
  border: 1px solid #475569;
  border-radius: 8px;
  background: rgba(13, 18, 29, 0.97);
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.34);
  color: #e2e8f0;
  font-size: 12px;
  pointer-events: none;
}
.turnover-tooltip-title { margin-bottom: 7px; color: #fff; font-size: 13px; font-weight: 800; }
.turnover-tooltip-row { display: flex; justify-content: space-between; gap: 24px; margin-top: 4px; }
.turnover-tooltip-row.total { padding-bottom: 6px; border-bottom: 1px solid #334155; color: #fff; }
.turnover-tooltip-row.forecast { color: #fef08a; font-weight: 700; }
.turnover-tooltip-row strong { color: #f8fafc; }
.turnover-tooltip-marker { display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 50%; }
.turnover-tooltip-marker.sh { background: #ef4444; }
.turnover-tooltip-marker.sz { background: #f97316; }
.turnover-tooltip-marker.bj { background: #fca5a5; }
.turnover-tooltip-change { margin-top: 8px; font-weight: 700; }
.turnover-tooltip-change.up { color: #f87171; }
.turnover-tooltip-change.down { color: #4ade80; }
.turnover-tooltip-change.flat { color: #cbd5e1; }

.emotion-cycle-section {
  margin-bottom: 30px;
}

.margin-sentiment-section {
  margin-bottom: 22px;
}

.margin-sentiment-note {
  margin: 4px 0 0;
  color: rgba(226, 232, 240, 0.78);
  font-size: 12px;
}

.margin-sentiment-date {
  color: #cbd5e1;
  font-size: 13px;
}

.margin-sentiment-card,
.margin-sentiment-empty {
  border: 1px solid rgba(148, 163, 184, 0.3);
  border-radius: 8px;
  background: #1b2431;
}

.margin-sentiment-card {
  position: relative;
  padding: 14px 16px 10px;
}

.margin-sentiment-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.margin-sentiment-metrics div {
  display: grid;
  gap: 5px;
}

.margin-sentiment-metrics span {
  color: #9fb0c8;
  font-size: 12px;
}

.margin-sentiment-metrics strong {
  color: #f8fafc;
  font-size: 20px;
}

.margin-sentiment-metrics .positive strong { color: #fb7185; }
.margin-sentiment-metrics .negative strong { color: #4ade80; }

.margin-chart-title {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 14px;
  color: #cbd5e1;
  font-size: 12px;
}

.margin-chart-title span:last-child {
  color: #8191a8;
}

.margin-balance-chart,
.margin-change-chart {
  display: block;
  width: 100%;
  height: 150px;
  margin-top: 4px;
}

.margin-balance-chart text,
.margin-change-chart text {
  fill: #94a3b8;
  font-size: 11px;
}

.margin-axis-line,
.margin-zero-line {
  stroke: #64748b;
}

.margin-zero-line {
  stroke-dasharray: 4 4;
}

.margin-hover-line {
  stroke: rgba(255, 255, 255, 0.7);
  stroke-width: 1;
  stroke-dasharray: 3 3;
  pointer-events: none;
}

.margin-hover-surface {
  fill: transparent;
  cursor: crosshair;
}

.financing-balance-line {
  stroke: #fb7185;
  stroke-width: 2.4;
  vector-effect: non-scaling-stroke;
}

.securities-lending-line {
  stroke: #60a5fa;
  stroke-width: 2.2;
  vector-effect: non-scaling-stroke;
}

.margin-bar-up { fill: #fb7185; }
.margin-bar-down { fill: #4ade80; }
.lending-bar-up { fill: #60a5fa; }
.lending-bar-down { fill: #a78bfa; }

.margin-chart-legend {
  display: flex;
  gap: 16px;
  color: #b8c4d6;
  font-size: 11px;
}

.margin-chart-legend span::before {
  content: '';
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 5px;
  background: currentColor;
}

.margin-chart-legend .financing { color: #fb7185; }
.margin-chart-legend .lending { color: #60a5fa; }

.margin-hover-tooltip {
  position: absolute;
  z-index: 4;
  width: 250px;
  padding: 10px 12px;
  border: 1px solid rgba(148, 163, 184, 0.5);
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.97);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.36);
  color: #dbeafe;
  font-size: 12px;
  line-height: 1.5;
  pointer-events: none;
}

.margin-hover-date {
  margin-bottom: 7px;
  color: #f8fafc;
  font-weight: 700;
}

.margin-hover-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 12px;
}

.margin-hover-grid span {
  display: flex;
  flex-direction: column;
  color: #9fb0c8;
}

.margin-hover-grid b {
  color: #f8fafc;
  font-size: 13px;
}

.margin-hover-grid .positive b { color: #fb7185; }
.margin-hover-grid .negative b { color: #4ade80; }

.margin-sentiment-axis {
  display: flex;
  justify-content: space-between;
  color: #94a3b8;
  font-size: 11px;
}

.margin-sentiment-empty {
  padding: 18px;
  color: #94a3b8;
  font-size: 13px;
}

.emotion-phase-card {
  margin-bottom: 12px;
  border-radius: 14px;
  padding: 12px 14px;
  color: #f8fafc;
  background: rgba(15, 23, 42, 0.36);
  border: 1px solid rgba(203, 213, 225, 0.26);
  box-shadow: 0 8px 22px rgba(15, 23, 42, 0.22);
}

.emotion-phase-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.emotion-phase-name {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.emotion-phase-confidence {
  font-size: 12px;
  color: #dbeafe;
}

.emotion-phase-action {
  margin-top: 8px;
  font-size: 14px;
  font-weight: 600;
}

.emotion-phase-reason {
  margin-top: 6px;
  font-size: 12px;
  color: rgba(226, 232, 240, 0.92);
  line-height: 1.6;
}

.emotion-phase-stale {
  margin-top: 6px;
  font-size: 12px;
  color: #fef08a;
}

.emotion-intraday-note {
  margin: 0 0 10px;
  padding: 8px 12px;
  border: 1px solid rgba(96, 165, 250, 0.6);
  border-radius: 7px;
  background: rgba(30, 64, 175, 0.24);
  color: #dbeafe;
  font-size: 12px;
}

.phase-ice {
  border-color: rgba(56, 189, 248, 0.42);
  background: linear-gradient(135deg, rgba(12, 74, 110, 0.72), rgba(30, 41, 59, 0.82));
}

.phase-start {
  border-color: rgba(250, 204, 21, 0.42);
  background: linear-gradient(135deg, rgba(92, 53, 15, 0.70), rgba(30, 41, 59, 0.82));
}

.phase-ferment {
  border-color: rgba(74, 222, 128, 0.42);
  background: linear-gradient(135deg, rgba(6, 78, 59, 0.72), rgba(30, 41, 59, 0.82));
}

.phase-euphoria {
  border-color: rgba(248, 113, 113, 0.42);
  background: linear-gradient(135deg, rgba(127, 29, 29, 0.72), rgba(30, 41, 59, 0.82));
}

.phase-decline {
  border-color: rgba(244, 114, 182, 0.42);
  background: linear-gradient(135deg, rgba(112, 26, 117, 0.72), rgba(30, 41, 59, 0.82));
}

.chart-container {
  background: white;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

/* 情绪周期 */
.emotion-cycle-section {
  margin-bottom: 30px;
}

.emotion-cycle-chart {
  background:
    radial-gradient(circle at 18% 0%, rgba(78, 97, 150, 0.22), transparent 34%),
    linear-gradient(180deg, #20252f 0%, #171c26 100%);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 14px;
  padding: 10px 14px 8px;
  box-shadow: 0 20px 40px rgba(10, 14, 24, 0.32);
  min-height: 548px;
}

.emotion-cycle-topbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 8px;
}

.emotion-topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.emotion-granularity-switch {
  display: inline-flex;
  border: 1px solid rgba(148, 163, 184, 0.35);
  border-radius: 8px;
  overflow: hidden;
  background: rgba(15, 23, 42, 0.48);
  flex-shrink: 0;
}

.granularity-btn {
  border: none;
  background: transparent;
  color: #cbd5e1;
  font-size: 12px;
  font-weight: 700;
  padding: 5px 10px;
  cursor: pointer;
}

.granularity-btn.active {
  background: rgba(59, 130, 246, 0.26);
  color: #f8fafc;
}

.emotion-cycle-legend {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 4px;
}

.emotion-cycle-legend-items {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px 12px;
  min-width: 0;
  overflow: visible;
  flex: 1;
}

.emotion-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: none;
  background: transparent;
  color: #dde5f2;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 0;
  cursor: pointer;
  white-space: nowrap;
  opacity: 1;
}

.emotion-legend-item:hover {
  color: #ffffff;
}

.emotion-legend-item.inactive {
  opacity: 0.34;
}

.emotion-legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d6dce8;
  border: 1px solid rgba(255, 255, 255, 0.72);
  flex-shrink: 0;
}

.emotion-band-legend {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px 9px;
  max-width: 330px;
  flex-shrink: 0;
}

.band-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: rgba(226, 232, 240, 0.72);
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
}

.band-legend-item i {
  display: block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  box-shadow: 0 0 10px currentColor;
}

.emotion-legend-text {
  line-height: 1;
  letter-spacing: 0;
}

.emotion-legend-pager {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #c8d1eb;
  flex-shrink: 0;
}

.emotion-legend-page-btn {
  border: none;
  background: transparent;
  color: #c8d1eb;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}

.emotion-legend-page-btn:disabled {
  opacity: 0.35;
  cursor: default;
}

.emotion-legend-page-text {
  font-size: 11px;
  color: #c8d1eb;
}

.emotion-metric-strip {
  display: grid;
  grid-template-columns: repeat(8, minmax(76px, 1fr));
  gap: 6px;
  margin: 6px 0 8px;
}

.emotion-metric-chip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
  height: 26px;
  border: 1px solid color-mix(in srgb, var(--metric-color) 36%, rgba(255, 255, 255, 0.12));
  border-radius: 6px;
  background: color-mix(in srgb, var(--metric-bg) 72%, rgba(15, 23, 42, 0.62));
  padding: 0 8px;
}

.metric-name {
  min-width: 0;
  overflow: hidden;
  color: rgba(226, 232, 240, 0.86);
  font-size: 11px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.metric-value {
  color: var(--metric-color);
  font-size: 13px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.emotion-cycle-echart {
  width: 100%;
  height: 430px;
  min-height: 430px;
}

.emotion-cycle-canvas {
  position: relative;
  min-height: 452px;
  border-radius: 14px;
  overflow: hidden;
  background: linear-gradient(180deg, rgba(17, 24, 39, 0.18), rgba(17, 24, 39, 0.08));
}

.emotion-phase-timeline {
  margin-top: 10px;
  border-top: 1px solid rgba(148, 163, 184, 0.24);
  padding-top: 10px;
}

.phase-timeline-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  color: #cbd5e1;
  font-size: 12px;
}

.phase-timeline-track {
  display: grid;
  grid-template-columns: repeat(30, minmax(8px, 1fr));
  gap: 4px;
}

.phase-time-cell {
  min-height: 30px;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.2s ease, box-shadow 0.2s ease, opacity 0.2s ease;
  opacity: 0.92;
}

.phase-time-cell:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 12px rgba(15, 23, 42, 0.28);
  opacity: 1;
}

.phase-time-label {
  color: #f8fafc;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
  text-shadow: 0 1px 2px rgba(15, 23, 42, 0.42);
}

.emotion-cycle-svg {
  width: 100%;
  height: 452px;
  display: block;
}

.emotion-series-line {
  opacity: 0.92;
  filter: drop-shadow(0 0 4px rgba(255, 255, 255, 0.12));
}

.emotion-grid-line {
  stroke: rgba(255, 255, 255, 0.10);
  stroke-width: 1;
}

.emotion-axis-label {
  fill: #aeb8d2;
  font-size: 10px;
}

.emotion-axis-title {
  fill: #ecf1ff;
  font-size: 12px;
  font-weight: 600;
}

.emotion-band-label {
  fill: rgba(226, 232, 240, 0.46);
  font-size: 10px;
  font-weight: 700;
}

.emotion-hover-line {
  stroke: rgba(255, 255, 255, 0.28);
  stroke-width: 1;
  stroke-dasharray: 5 5;
}

.emotion-chart-tooltip {
  position: absolute;
  min-width: 168px;
  max-width: 216px;
  border-radius: 12px;
  padding: 10px 12px;
  background: rgba(250, 251, 253, 0.98);
  border: 1px solid rgba(15, 23, 42, 0.08);
  box-shadow: 0 14px 30px rgba(15, 23, 42, 0.22);
  pointer-events: none;
  z-index: 2;
}

.emotion-tooltip-title {
  font-size: 13px;
  font-weight: 700;
  color: #111827;
  margin-bottom: 8px;
}

.emotion-tooltip-row {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: #334155;
  line-height: 1.7;
}

.emotion-tooltip-marker {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.85);
  flex-shrink: 0;
}

.emotion-tooltip-name {
  flex: 1;
}

.emotion-tooltip-value {
  color: #0f172a;
  font-weight: 700;
  min-width: 56px;
  text-align: right;
}

/* 快捷入口 */
.quick-links {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
}

.quick-links .card {
  background: white;
  border-radius: 12px;
  padding: 24px;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
}

.quick-links .card:hover {
  transform: translateY(-4px);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.15);
}

.quick-links .card-icon {
  font-size: 36px;
  margin-bottom: 12px;
}

.quick-links h3 {
  color: #2d3748;
  font-size: 16px;
  margin: 0;
}

@media (max-width: 900px) {
  .margin-sentiment-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .margin-chart-title {
    flex-direction: column;
    gap: 2px;
  }

  .trend-bars {
    grid-template-columns: repeat(4, minmax(44px, 1fr));
    row-gap: 10px;
    min-height: auto;
  }

  .distribution-bars {
    grid-template-columns: repeat(3, minmax(40px, 1fr));
    row-gap: 14px;
    min-height: auto;
  }

  .emotion-cycle-chart {
    min-height: 520px;
    padding: 12px 10px 6px;
  }

  .emotion-cycle-topbar {
    flex-direction: column;
    gap: 8px;
  }

  .emotion-topbar-right {
    width: 100%;
    justify-content: space-between;
  }

  .emotion-cycle-legend {
    align-items: flex-start;
    flex-direction: column;
  }

  .emotion-band-legend {
    justify-content: flex-start;
    max-width: none;
  }

  .emotion-metric-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .emotion-cycle-echart {
    height: 390px;
    min-height: 390px;
  }

  .emotion-cycle-canvas {
    min-height: 420px;
  }
}
</style>
