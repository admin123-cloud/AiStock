# 通达信量化平台 SDK 安装指南

## ⚠️ 重要说明

通达信量化平台 SDK（TdxQuant）**不是通过 PyPI 安装的**，而是需要从通达信 QMT 客户端下载。

## 目录说明

### 1. **策略文件目录**（固定，不可更改）

- **路径：** `通达信客户端安装目录/PYPlugins/user/`
- **用途：** 存放策略文件（.py）
- **限制：** 策略文件**必须**在这个目录下运行
- **说明：** 这是通达信客户端的硬性要求，**无法更改**
- **示例：** `C:\通达信\PYPlugins\user\my_strategy.py`

### 2. **xtquant SDK 目录**（可移动）

- **路径：** Python 环境的 `site-packages/xtquant`
- **用途：** 存放 SDK 工具包
- **限制：** 可以放在 Python 环境的任何位置，只要能被导入即可
- **说明：** 这是 Python 包，**可以自由移动**
- **示例：** `F:\Stock\AiStock\venv\Lib\site-packages\xtquant`

### 🎯 重要区别

| 项目 | 策略文件目录 | xtquant SDK 目录 |
|------|-------------|-----------------|
| **位置** | 通达信客户端安装目录 | Python 环境 |
| **是否可更改** | ❌ 不可更改 | ✅ 可以移动 |
| **开发要求** | 策略文件必须在此目录运行 | SDK 只需能被导入 |
| **用途** | 运行通达信策略 | 开发自定义程序 |

## 安装步骤

### 1. 下载并安装通达信金融终端

1. 访问通达信官网：https://www.tdx.com.cn/
2. 下载最新版本的通达信金融终端
3. 按照提示完成软件安装

### 2. 开通量化交易权限

1. 安装完成后，登录通达信软件
2. 进入主界面，找到"量化分析"菜单
3. 选择"量化交易功能"并开启
4. 确认量化交易权限已开通

### 3. 下载 xtquant 工具包

1. 在通达信 QMT 客户端中，找到"知识库"板块
2. 下载 xtquant 工具包（压缩文件）

### 4. 安装 xtquant 到 Python 环境

1. 解压 xtquant 压缩包，得到 xtquant 文件夹
2. 将 xtquant 文件夹放入你的 Python 环境

#### Python 环境路径示例：

**Anaconda 环境：**
```
C:\Users\用户名\anaconda3\Lib\site-packages\xtquant
```

**虚拟环境（venv）：**
```
F:\Stock\AiStock\venv\Lib\site-packages\xtquant
```

**系统 Python：**
```
C:\Python313\Lib\site-packages\xtquant
```

### 5. 重启通达信客户端

完成上述步骤后，重启通达信客户端以确保 SDK 正确加载。

## 🎯 开发位置说明

### 策略文件（必须在 /PYPlugins/user/ 目录）

**❌ 不可更改位置**

- **要求**：通达信客户端的硬性要求
- **路径**：`通达信客户端安装目录/PYPlugins/user/`
- **说明**：策略文件（.py）**必须**在这个目录下运行
- **限制**：无法更改，无法移动

### 自定义程序（可在任意位置）

**✅ 可以自由开发**

- **要求**：只需能导入 xtquant SDK
- **路径**：项目根目录（如 `F:\Stock\AiStock\`）
- **说明**：自定义程序可以在任何位置开发
- **限制**：无，只要 Python 能找到 xtquant 包即可

### 示例对比

| 类型 | 位置 | 是否可更改 | 用途 |
|------|------|-----------|------|
| **策略文件** | `/PYPlugins/user/` | ❌ 不可更改 | 在通达信客户端中运行 |
| **自定义程序** | 项目根目录 | ✅ 可以更改 | 开发自定义应用 |

**本项目情况：**

本项目是**自定义程序**，不是通达信策略，因此：
- ✅ **可以**在项目根目录（`F:\Stock\AiStock\`）开发
- ✅ **可以**使用 VSCode、PyCharm 等 IDE
- ✅ **可以**使用虚拟环境
- ✅ **只需** xtquant SDK 在 Python 环境中即可

## 🔍 验证安装

在 Python 中验证安装：

```python
from tqcenter import tq
print("通达信量化平台 SDK 安装成功！")
```

如果成功导入，说明安装正确。

## 📖 官方文档

- 通达信量化平台文档：https://help.tdx.com.cn/quant/
- 初始化文档：https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1cv85e8u9nb0c.html
- K线数据文档：https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h10g60jt68sc.html
- 股票信息文档：https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h10jj7r7jol4.html

## ⚙️ 配置数据源

在 `config/datasources.yaml` 中启用通达信量化平台数据源：

```yaml
pytdx_quant:
  enabled: true  # 启用数据源
  priority: 0  # 最高优先级
  name: "通达信量化平台"
  description: "通达信量化平台官方API数据源"
  
  data_config:
    dividend_type: "none"  # 复权类型
    fill_data: true
    max_count: 24000
```

## 🚀 使用示例

```python
from data_fetcher import DataSourceManager

# 获取数据源管理器实例
manager = DataSourceManager()

# 获取股票历史数据
data = manager.get_stock_history('000001.SZ', '2024-01-01', '2024-12-31', 'daily')

# 获取股票信息
info = manager.get_stock_info('000001.SZ')

# 获取实时行情
quote = manager.get_stock_quote('000001.SZ')
```

## ⚠️ 注意事项

1. **必须先安装通达信客户端**：xtquant SDK 依赖通达信客户端运行
2. **需要开通量化权限**：在通达信客户端中开通量化交易功能
3. **Python 版本要求**：推荐使用 Python 3.10 或更高版本
4. **数据限制**：单次最多返回 24000 条数据
5. **初始化要求**：必须调用 `tq.initialize(__file__)` 进行初始化
6. **股票列表**：该 API 暂未提供股票列表功能，建议结合其他数据源使用

## 🆚 故障排除

### 导入失败

如果出现 `ImportError: No module named 'tqcenter'` 错误：

1. 检查 xtquant 文件夹是否正确放入 Python 环境
2. 确认 Python 环境路径是否正确
3. 尝试重启 Python 解释器
4. 检查是否使用了虚拟环境

### 初始化失败

如果初始化失败：

1. 确认通达信客户端正在运行
2. 检查是否已开通量化交易权限
3. 尝试重启通达信客户端
4. 查看通达信客户端日志

### 获取数据失败

如果无法获取数据：

1. 检查网络连接
2. 确认股票代码格式正确（如 000001.SZ）
3. 查看通达信客户端是否正常连接
4. 检查数据源配置中的参数是否正确

## 📞 技术支持

如遇到问题，请参考：
- 通达信官方文档：https://help.tdx.com.cn/quant/
- 通达信客服：https://www.tdx.com.cn/
