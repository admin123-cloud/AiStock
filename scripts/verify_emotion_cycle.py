"""验证情绪周期数据"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from sqlalchemy import func
from utils.database import db
from utils.logger import get_logger
from models.stock_models import EmotionCycle

logger = get_logger("VerifyEmotionCycle")


def verify_emotion_cycle(days=30):
    """验证情绪周期数据"""
    session = next(db.get_session())
    
    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)
        
        logger.info(f"验证日期范围: {start_date} 到 {end_date}")
        
        # 获取情绪周期数据
        emotion_cycles = session.query(EmotionCycle).filter(
            EmotionCycle.date >= start_date,
            EmotionCycle.date <= end_date
        ).order_by(EmotionCycle.date.desc()).all()
        
        logger.info(f"共找到 {len(emotion_cycles)} 条情绪周期数据")
        
        if not emotion_cycles:
            logger.warning("未找到任何情绪周期数据")
            return
        
        print("\n" + "="*120)
        print(f"{'日期':<12} {'收盘上涨率':<12} {'上证':<8} {'深证':<8} {'创业板':<10} "
              f"{'昨日妖股':<12} {'昨日强势':<12} {'昨日弱势':<12} {'股票数':<8}")
        print("="*120)
        
        for ec in emotion_cycles:
            print(f"{ec.date.strftime('%Y-%m-%d'):<12} "
                  f"{float(ec.close_up_rate):<12.2f} "
                  f"{float(ec.sh_up_rate):<8.2f} "
                  f"{float(ec.sz_up_rate):<8.2f} "
                  f"{float(ec.cyb_up_rate):<10.2f} "
                  f"{float(ec.yesterday_monster_up_rate):<12.2f} "
                  f"{float(ec.yesterday_strong_up_rate):<12.2f} "
                  f"{float(ec.yesterday_weak_up_rate):<12.2f} "
                  f"{ec.total_stocks:<8}")
        
        print("="*120)
        
        # 统计信息
        avg_close_up_rate = sum(float(ec.close_up_rate) for ec in emotion_cycles) / len(emotion_cycles)
        avg_sh_up_rate = sum(float(ec.sh_up_rate) for ec in emotion_cycles) / len(emotion_cycles)
        avg_sz_up_rate = sum(float(ec.sz_up_rate) for ec in emotion_cycles) / len(emotion_cycles)
        avg_cyb_up_rate = sum(float(ec.cyb_up_rate) for ec in emotion_cycles) / len(emotion_cycles)
        
        print(f"\n统计信息:")
        print(f"  数据条数: {len(emotion_cycles)}")
        print(f"  平均收盘上涨率: {avg_close_up_rate:.2f}%")
        print(f"  平均上证上涨率: {avg_sh_up_rate:.2f}%")
        print(f"  平均深证上涨率: {avg_sz_up_rate:.2f}%")
        print(f"  平均创业板上涨率: {avg_cyb_up_rate:.2f}%")
        
        # 最新数据
        latest = emotion_cycles[0]
        print(f"\n最新数据 ({latest.date.strftime('%Y-%m-%d')}):")
        print(f"  收盘上涨率: {float(latest.close_up_rate):.2f}%")
        print(f"  上证上涨率: {float(latest.sh_up_rate):.2f}%")
        print(f"  深证上涨率: {float(latest.sz_up_rate):.2f}%")
        print(f"  创业板上涨率: {float(latest.cyb_up_rate):.2f}%")
        print(f"  昨日妖股上涨率: {float(latest.yesterday_monster_up_rate):.2f}%")
        print(f"  昨日强势股上涨率: {float(latest.yesterday_strong_up_rate):.2f}%")
        print(f"  昨日弱势股上涨率: {float(latest.yesterday_weak_up_rate):.2f}%")
        print(f"  股票总数: {latest.total_stocks}")
        
        return {
            'count': len(emotion_cycles),
            'avg_close_up_rate': avg_close_up_rate,
            'avg_sh_up_rate': avg_sh_up_rate,
            'avg_sz_up_rate': avg_sz_up_rate,
            'avg_cyb_up_rate': avg_cyb_up_rate
        }
        
    finally:
        session.close()


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='验证情绪周期数据')
    parser.add_argument('days', type=int, nargs='?', default=30, help='验证天数，默认30天')
    
    args = parser.parse_args()
    
    verify_emotion_cycle(args.days)
