import { use, init as initChart } from 'echarts/core'
import 'echarts/theme/v5.js'
import { LineChart, BarChart, PieChart, CandlestickChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, TitleComponent, DataZoomComponent,
  MarkLineComponent, MarkPointComponent, MarkAreaComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, BarChart, PieChart, CandlestickChart, ScatterChart, GridComponent,
  TooltipComponent, LegendComponent, TitleComponent, DataZoomComponent, MarkLineComponent,
  MarkPointComponent, MarkAreaComponent, VisualMapComponent, CanvasRenderer])
// Preserve the established v5 layout while accepting explicit page themes.
export function init(dom, theme, options) {
  return initChart(dom, theme == null ? 'v5' : theme, options)
}
export { getInstanceByDom, dispose } from 'echarts/core'
