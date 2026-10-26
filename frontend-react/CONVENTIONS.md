# frontend-react 约定（React + Catalyst）

本目录是 AiStock 当前唯一前端实现：React 19 + TypeScript + Vite + Tailwind v4 + Catalyst。
旧 Vue3 + Element Plus 实现已退出运行入口并已删除；React 是唯一受支持的前端实现。

## 技术栈与目录

- React 19 + react-router-dom v7（`BrowserRouter`，路由表见 `src/router/index.tsx`，勿改路径与 meta 语义）
- UI 组件：`src/components/catalyst/`（Button/Input/Table/Dialog/Dropdown/Badge/Alert/…，官方 Catalyst TS 版）
- 通知：`useToast()`（组件内）或 `toast`（`src/components/toast.tsx`，替代 ElMessage）
- 请求：`@/utils/request`（axios 实例，baseURL `/api`，响应拦截器已解包 `data`，错误自动 toast；自定义配置 `skipErrorHandler: true` 跳过）
- API 模块：`src/api/*.ts`（与旧版同名同签名，直接调用）
- 页面：`src/views/<组>/<Name>.tsx`，默认导出组件；占位页逐步替换为真实实现
- 图表：ECharts 6（封装参考旧 `LocalKLineChart.vue`）、lightweight-charts 5
- 数据/工具：`src/data/gen2Alpha191.ts`、`src/utils/g3StatusText.ts` 已复制

## 样式与暗色模式

- **页面级 UI 原语（必须优先使用）**：`@/components/ui` 提供
  - `PageHeader`（页面头：title/subtitle/actions）
  - `Card`（统一卡片：rounded-lg + ring，亮暗双配色）、`SectionTitle`（节标题，左对齐，禁用 border-l-4 竖条与居中节标题）
  - `StatCard`（指标大卡：label/value/tone/hint，tone 支持 up/down/warning/info）
  - `ChangeValue`（涨跌数字：红涨绿跌 + ↑↓箭头 + tabular-nums，value/pct）
  - `EmptyState`（空态）、`PageLoading`（加载态）
- 全部使用 Tailwind 工具类；暗色用 `dark:` 前缀（`<html class="dark">` 切换，见 `src/hooks/useDarkMode.ts`）
- 每个页面必须同时适配亮/暗两套配色（数据表格、徽章、图表配色均需）
- A股语义色：红涨绿跌。行情涨跌数字优先用 `ChangeValue`；手写时涨 `text-red-600 dark:text-red-400`，跌 `text-green-600 dark:text-green-500`；数字用 `tabular-nums`
- ECharts 图表需监听暗色切换重建（`EChart` 组件已内置，背景已透明）

## 组件映射（Element Plus → Catalyst）

| Element Plus | React 替代 |
|---|---|
| el-button | `Button`（color/outline/plain） |
| el-table | `Table/TableHead/TableBody/TableRow/TableHeader/TableCell` + 自行实现排序（本地排序即可，保持旧页行为） |
| el-tag | `Badge`（color） |
| el-dialog | `Dialog/DialogTitle/DialogBody/DialogActions` |
| el-select | `Select`（原生）或 `Listbox` |
| el-form/el-input | `Field/Label/Input`、`Fieldset` |
| el-message | `useToast()` |
| el-pagination | `Pagination` 系列 |
| el-tabs | 自行用 `Button plain` + 状态，或 Headless Tab（保持旧页交互） |
| el-progress / el-tooltip 等 | Tailwind 自绘或简化实现，以功能不减为准 |

## 页面迁移规则

1. 需要核对历史行为时读取迁移门禁记录和 `research/catalog.json` 的历史索引，保持：路由参数、API 调用、轮询间隔、刷新逻辑、交互（筛选/排序/分页/跳转）一致
2. 状态：`useState/useEffect/useMemo`；轮询必须 `return () => clearInterval(...)` 清理
3. 跳转用 `useNavigate()`；链接用 `<Link to>`（react-router-dom）
4. 路由参数用 `useParams()`；查询串用 `useSearchParams()`
5. 默认导出函数组件，命名与文件名一致
6. 完成一个页面后 `npx tsc -b` 必须零错误
7. 不引入新依赖；Element Plus 不得出现在新代码

## 已知约束

- 前端必须能在后端未就绪时打开（错误被 toast 吞掉但页面不崩）——知识库 LEARNINGS 2026-09-09
- dev 端口 3100（避免与旧前端 3000 冲突）；`/api` 代理到 8000

## 迁移状态（2026-10-23）

- React 已覆盖原 Vue 的 28 条路由，并补齐 `/kline-check` 的自动循环、单条/批量修复、修复结果导出、历史筛选分页和详情明细。
- `docker-compose.dev.yml`、`docker-compose.app.yml`、`docker-compose.release.yml` 和 `restart_services.bat` 均以 React 为入口。
- Vue 活动代码与专用归档已删除；历史 worktree/研究备份不属于当前前端入口。
