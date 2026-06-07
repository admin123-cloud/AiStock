# AiStock 文档整理方案

## 当前md文件分析

项目根目录下共有15个md文件，可分为以下几类：

### 1. 核心文档（保留在根目录）
- **README.md** - 项目主文档（必须保留）
- **TODOList.md** - 待办事项（保留，定期更新）

### 2. 功能实现文档（移至 docs/guides/）
- SECTOR_KLINE_QUICKSTART.md
- LAZY_LOADING_KLINE.md
- 情绪周期图表功能说明.md
- STOCK_LIST_KLINE_UPDATE.md
- STOCKS_KLINE_GUIDE.md

### 3. 问题修复文档（移至 docs/fixes/）
- fix_kline_issue.md
- kline-chart-debug-guide.md
- verify-chart-container.md

### 4. 数据源集成文档（移至 docs/integrations/）
- pytdx_integration.md
- Intraday_Tushare_Realtime_Summary.md
- Tushare_Kline_Sync_Summary.md

### 5. API重构文档（移至 docs/api/）
- API接口重构说明.md

### 6. 限流优化文档（移至 docs/optimizations/）
- TickFlow限流修复说明.md
- TickFlow分桶限流修复说明.md

---

## 建议的目录结构

```
AiStock/
├── README.md                    # 项目主文档
├── TODOList.md                  # 待办事项
├── docs/                        # 文档目录
│   ├── README.md                # 文档索引
│   ├── guides/                 # 功能实现指南
│   │   ├── sector-kline-quickstart.md
│   │   ├── lazy-loading-kline.md
│   │   ├── emotion-cycle.md
│   │   ├── stock-list-kline.md
│   │   └── stocks-kline-guide.md
│   ├── fixes/                  # 问题修复记录
│   │   ├── kline-issue-fix.md
│   │   ├── kline-chart-debug.md
│   │   └── chart-container-verify.md
│   ├── integrations/           # 数据源集成
│   │   ├── pytdx-integration.md
│   │   ├── intraday-tushare.md
│   │   └── tushare-kline-sync.md
│   ├── api/                    # API文档
│   │   └── api-refactoring.md
│   └── optimizations/          # 性能优化
│       ├── tickflow-rate-limit.md
│       └── tickflow-bucket-limit.md
├── backend/
├── frontend/
└── config/
```

---

## 整理后的文档内容索引

### 功能实现指南 (docs/guides/)

1. **sector-kline-quickstart.md**
   - 板块K线数据快速开始
   - 包含字段说明、快速启动命令、API使用示例

2. **lazy-loading-kline.md**
   - K线懒加载功能实现
   - 包含前端/后端修改、自动检测脚本、使用流程

3. **emotion-cycle.md**
   - 情绪周期图表功能
   - 包含API接口、前端功能、数据来源、技术实现

4. **stock-list-kline.md**
   - 股票列表最新K线数据显示
   - 包含前后端修改、页面效果、API说明

5. **stocks-kline-guide.md**
   - 股票列表K线数据显示测试指南
   - 包含修改步骤、预期效果、测试方法

### 问题修复记录 (docs/fixes/)

1. **kline-issue-fix.md**
   - K线图表和涨跌幅问题修复方案
   - 包含问题现象、原因分析、解决步骤

2. **kline-chart-debug.md**
   - K线图表不显示完整排查指南
   - 包含排查步骤、常见问题、快速测试命令

3. **chart-container-verify.md**
   - K线图表容器验证步骤
   - 包含修复内容、验证步骤、诊断代码

### 数据源集成 (docs/integrations/)

1. **pytdx-integration.md**
   - pytdx实时行情集成说明
   - 包含改进对比、文件变更、API说明、注意事项

2. **intraday-tushare.md**
   - 盘中实时K线查询升级总结
   - 包含升级背景、解决方案、实施结果、技术实现

3. **tushare-kline-sync.md**
   - Tushare K线数据同步总结
   - 包含问题背景、解决方案、数据格式对齐、实施结果

### API文档 (docs/api/)

1. **api-refactoring.md**
   - API接口重构说明
   - 包含变更内容、现有接口、前端调用变更

### 性能优化 (docs/optimizations/)

1. **tickflow-rate-limit.md**
   - TickFlow API限流修复说明
   - 包含问题描述、解决方案、关键问题解答、性能影响

2. **tickflow-bucket-limit.md**
   - TickFlow API分桶限流修复说明
   - 包含分桶限流实现、优势、实际应用场景

---

## 整理建议

### 立即执行

1. **创建docs目录结构**
   ```bash
   mkdir -p docs/guides docs/fixes docs/integrations docs/api docs/optimizations
   ```

2. **移动文档文件**
   ```bash
   # 功能实现
   mv SECTOR_KLINE_QUICKSTART.md docs/guides/sector-kline-quickstart.md
   mv LAZY_LOADING_KLINE.md docs/guides/lazy-loading-kline.md
   mv "情绪周期图表功能说明.md" docs/guides/emotion-cycle.md
   mv STOCK_LIST_KLINE_UPDATE.md docs/guides/stock-list-kline.md
   mv STOCKS_KLINE_GUIDE.md docs/guides/stocks-kline-guide.md
   
   # 问题修复
   mv fix_kline_issue.md docs/fixes/kline-issue-fix.md
   mv kline-chart-debug-guide.md docs/fixes/kline-chart-debug.md
   mv verify-chart-container.md docs/fixes/chart-container-verify.md
   
   # 数据源集成
   mv pytdx_integration.md docs/integrations/pytdx-integration.md
   mv Intraday_Tushare_Realtime_Summary.md docs/integrations/intraday-tushare.md
   mv Tushare_Kline_Sync_Summary.md docs/integrations/tushare-kline-sync.md
   
   # API文档
   mv API接口重构说明.md docs/api/api-refactoring.md
   
   # 性能优化
   mv TickFlow限流修复说明.md docs/optimizations/tickflow-rate-limit.md
   mv TickFlow分桶限流修复说明.md docs/optimizations/tickflow-bucket-limit.md
   ```

3. **创建文档索引**
   - 创建 `docs/README.md` 作为文档导航索引
   - 更新主 `README.md` 中的文档链接

### 后续维护

1. **定期清理过时文档**
   - 每季度检查一次文档的有效性
   - 删除或归档不再需要的文档

2. **统一命名规范**
   - 使用小写字母和连字符：`feature-name.md`
   - 避免空格和中文命名（除非是用户文档）

3. **建立文档规范**
   - 所有新文档必须包含：标题、概述、详细内容、更新日期
   - 重要修改需要在文档中标注

4. **添加README索引**
   ```markdown
   # AiStock 文档目录
   
   ## 快速导航
   
   - [功能实现指南](guides/) - 各功能的开发和使用指南
   - [问题修复记录](fixes/) - 已解决问题的详细记录
   - [数据源集成](integrations/) - 各数据源的集成说明
   - [API文档](api/) - API接口相关文档
   - [性能优化](optimizations/) - 性能优化和限流策略
   ```

---

## 整理后的好处

1. **根目录更清晰**
   - 从15个文件减少到2个核心文件
   - 减少视觉混乱，更容易找到重要文件

2. **文档分类明确**
   - 按功能类型分类，查找更高效
   - 避免文件名相似导致的混淆

3. **便于维护**
   - 分类管理，便于批量操作
   - 新文档有明确的放置位置

4. **更好的协作**
   - 团队成员可以快速定位需要的文档
   - 减少重复工作和沟通成本

5. **便于自动化**
   - 可以编写脚本自动生成文档导航
   - 便于CI/CD集成文档检查

---

## 注意事项

1. **不要删除原文件**
   - 移动前先备份
   - 确认移动成功后再删除原文件

2. **更新内部链接**
   - 检查文档之间的相互引用
   - 更新所有指向旧位置的链接

3. **通知团队成员**
   - 整理完成后通知所有成员
   - 说明新的文档组织结构

4. **版本控制**
   - 使用Git追踪移动操作
   - 提交信息应说明整理内容
