import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/catalyst/button'
import { PageHeader } from '@/components/ui'
import AR3DualCorePanel from './AR3DualCorePanel'

export default function MainwaveDaily() {
  const navigate = useNavigate()

  return (
    <div className="flex w-full flex-col gap-4">
      <PageHeader
        title="AR4.5 基本面选股与 AI 排雷中心"
        subtitle="AR4.5-L3b 唯一权威选股视图：进攻期按国家队分位动态 0/35/50/65% AB混合（F3A 绩优回踩 + F2A 业绩趋势主升）+ 宽基 ETF；防守/平衡期 100% 非A股风险调整 Top2 逆波 ETF + 南向港科滤网（严禁买 A 股个股）。"
        actions={
          <Button outline onClick={() => navigate('/g3/desk')}>
            前往实战作战台预填 →
          </Button>
        }
      />

      {/* 唯一权威主视图：AR4.5-L3b 终极双核作战雷达 */}
      <AR3DualCorePanel
        onPrefillOrder={() => {
          navigate('/g3/desk')
        }}
      />
    </div>
  )
}
