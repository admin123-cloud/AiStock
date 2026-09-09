import { use } from 'echarts/core'
import { LineChart, BarChart, PieChart, CandlestickChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, TitleComponent, DataZoomComponent,
  MarkLineComponent, MarkPointComponent, MarkAreaComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, BarChart, PieChart, CandlestickChart, ScatterChart, GridComponent,
  TooltipComponent, LegendComponent, TitleComponent, DataZoomComponent, MarkLineComponent,
  MarkPointComponent, MarkAreaComponent, VisualMapComponent, CanvasRenderer])
export { init, getInstanceByDom, dispose } from 'echarts/core'
