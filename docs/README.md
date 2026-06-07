# AiStock 文档目录

本文档目录包含 AiStock 项目的所有文档，按照功能分类组织。

## 目录结构

```
docs/
├── README.md                   # 本文档
├── architecture/               # 架构文档
│   ├── ENTERPRISE_ARCHITECTURE.md    # 企业架构设计
│   └── PROJECT_STRUCTURE_RECOMMENDATION.md  # 项目结构建议
├── guides/                     # 指南文档
│   ├── REFACTORING_GUIDE.md    # 项目重构指南
│   ├── REFACTORING_SUMMARY.md  # 重构总结
│   └── MIGRATION_GUIDE.md      # 迁移指南
└── references/                 # 参考文档
    ├── DATA_SOURCES_ANALYSIS.md # 数据源分析
    └── DOCS_ORGANIZATION.md     # 文档组织说明
```

## 快速导航

### 架构文档
- [企业架构设计](architecture/ENTERPRISE_ARCHITECTURE.md) - 系统的整体架构设计
- [项目结构建议](architecture/PROJECT_STRUCTURE_RECOMMENDATION.md) - 推荐的目录结构

### 指南文档
- [项目重构指南](guides/REFACTORING_GUIDE.md) - 详细的重构说明和迁移指南
- [重构总结](guides/REFACTORING_SUMMARY.md) - 重构工作的总结
- [迁移指南](guides/MIGRATION_GUIDE.md) - 从旧版本迁移的说明

### 参考文档
- [数据源分析](references/DATA_SOURCES_ANALYSIS.md) - 各种数据源的详细分析
- [文档组织说明](references/DOCS_ORGANIZATION.md) - 文档组织结构说明

## 根目录保留文件

根目录仅保留以下核心文档：

- **README.md** - 项目主文档，包含项目概述、快速开始等
- **TODOList.md** - 待办事项列表
- **.env.example** - 环境变量配置示例
- **.gitignore** - Git 忽略文件配置

## 使用说明

1. **新用户**：先阅读根目录的 README.md，然后查看 guides/ 目录下的指南
2. **开发者**：查看 architecture/ 目录了解系统架构
3. **维护者**：参考 references/ 目录获取技术细节

## 文档更新

如需更新文档，请遵循以下原则：

1. 架构相关文档放入 `architecture/` 目录
2. 使用指南放入 `guides/` 目录
3. 技术参考文档放入 `references/` 目录
4. 保持根目录简洁，只保留核心文档
