import React, { useEffect, useState } from 'react'
import { getSentimentRadar } from '@/api/marketBoard'
import { Card } from '@/components/ui'
import SentimentRadar from '@/components/SentimentRadar'
import SectorHeatPanel from '@/components/SectorHeatPanel'
import AuctionAnomalyPanel from '@/components/AuctionAnomalyPanel'

// eslint-disable-next-line @typescript-eslint/no-unused-vars
type _RadarData = any

/**
 * R56 市场看板三件套容器组件（嵌入首页情绪区旁）
 */
export default function R56MarketDashboardBlocks() {
  const [radarData, setRadarData] = useState<any>(null)
  const [radarLoading, setRadarLoading] = useState(true)

  useEffect(() => {
    setRadarLoading(true)
    getSentimentRadar()
      .then((res: any) => setRadarData(res))
      .catch(() => setRadarData(null))
      .finally(() => setRadarLoading(false))
  }, [])

  return (
    <div className="space-y-4">
      {/* 三列布局：情绪雷达 / 板块热度 / 竞价异动 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* 情绪雷达（占 1 列） */}
        <div className="lg:col-span-1">
          <Card title="情绪雷达" className="h-full">
            <SentimentRadar data={radarData} loading={radarLoading} />
          </Card>
        </div>

        {/* 概念/行业热度（占 2 列） */}
        <div className="lg:col-span-2">
          <Card title="板块热度" className="h-full">
            <SectorHeatPanel compact />
          </Card>
        </div>

        {/* 竞价异动（占 1 列） */}
        <div className="lg:col-span-1">
          <Card title="竞价异动" className="h-full">
            <AuctionAnomalyPanel />
          </Card>
        </div>
      </div>
    </div>
  )
}