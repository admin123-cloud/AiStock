# TdxQuant 通达信量化平台文档

## 1. 平台简介

TdxQuant是由深圳市财富趋势科技股份有限公司研发的专业量化投研平台，专注于为国内量化投资者提供从策略研究到投资决策的全流程解决方案。平台以高效、简洁为核心设计理念，致力于降低量化交易门槛，提升策略开发与执行的效率。

依托通达信近三十余年在金融科技领域的深厚积累，TdxQuant集成了完备的实时和历史行情数据、金融数据库及稳定的交易系统基础设施，为策略的研发、回测、验证和执行提供了坚实可靠的技术支持。

## 2. 运行环境要求

- 支持 64 位 Python 3.7、3.8、3.9、3.10、3.11、3.12、3.13、3.14等版本
- 系统会自动适配当前 Python 版本，建议使用3.13版本
- 运行 TdxQuant 程序前，需预先启动支持TQ策略功能的 通达信金融终端、量化模拟版或专业研究版等版本

## 3. 核心运行逻辑

TdxQuant 以 tqcenter 行情模块为核心，专注于为量化交易者提供高效、直接的数据服务，主要包含以下内容：

- **行情数据**：实时与历史的快照、K 线、分笔（Tick）数据
- **基本面数据**：除权除息、基本财务、专业财务、股票交易数据、市场数据等
- **新股和合约等信息**：标的基础信息、可转债、新股申购等
- **分类数据**：市场类型、行业分类、自定义板块等

## 4. 核心应用场景

### 4.1 策略研发与历史回测
平台提供"即用型"标准化数据。所有历史与实时数据均在服务端完成清洗、对齐，并预加载至客户端。支持用户快速获取指定时间维度的历史数据，并进行策略信号计算与回测分析。既提供复权因子，也提供各种类型的复权后的数据。

### 4.2 实时监控与信号预警
支持实时行情数据订阅，用户可基于自定义的指标与因子模型进行在线计算。当预设条件触发时，系统通过信号接口实时推送预警信息至客户端，助力研究者及时捕捉市场动态与交易机会。

### 4.3 交易模拟与实盘执行
平台构建了完整的策略交易闭环，提供模拟交易、券商实盘等两种执行环境：
- **模拟交易**：在仿真市场环境中，使用实时行情数据对策略进行持续跟踪与验证，评估其实际表现，全程无资金风险。
- **实盘交易**：通过稳定的交易总线，安全对接券商报盘系统，实现策略信号的自动化、高可靠性下单与交易管理。

## 5. API 接口文档

### 5.1 初始化与基础功能

#### tq.initialize(__file__)
- **功能**：初始化TQ连接，所有策略连接通达信客户端都必须调用此函数
- **参数**：`__file__` - 当前脚本文件路径
- **返回值**：无

#### tq.refresh_cache(force=False, market='')
- **功能**：刷新行情缓存，刷新后5分钟内取最新report和k线数据不会触发刷新
- **参数**：
  - `force`：是否强制刷新
  - `market`：指定市场刷新
- **返回值**：刷新结果

#### tq.refresh_kline(stock_list, period)
- **功能**：缓存历史K线，目前仅支持1m 5m 1d三种类型数据
- **参数**：
  - `stock_list`：股票代码列表
  - `period`：K线周期，如'1d'、'5m'等
- **返回值**：刷新结果

### 5.2 行情数据获取

#### tq.get_market_data(field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data)
- **功能**：根据股票，获取历史行情数据
- **参数**：
  - `field_list`：字段筛选，传空则返回全部
  - `stock_list`：证券代码列表（必填）
  - `period`：周期（必填），如1d/1w/1m/5m/15m/30m/60m等
  - `start_time`：起始时间，格式为YYYYMMDD或YYYYMMDDHHMMSS
  - `end_time`：结束时间，格式为YYYYMMDD或YYYYMMDDHHMMSS
  - `count`：返回数据个数（每只股票），<=0时返回start_time和end_time之间的全部数据
  - `dividend_type`：复权类型，none-不复权、front-前复权、back-后复权
  - `fill_data`：是否向后填充空缺数据，默认为True
- **返回值**：返回dict，{ field1 : value1, field2 : value2, ... }
  - field1, field2, ... ：数据字段
  - value1, value2, ... ：pd.DataFrame 数据集，index为stock_list，columns为time_list
- **返回字段说明**：
  | 字段 | 是否默认返回 | 数据类型 | 数据说明 |
  |------|-------------|----------|----------|
  | Date | Y | str | 日期 |
  | Time | Y | str | 时间 |
  | Open | Y | str | 开盘价 |
  | High | Y | str | 最高价 |
  | Low | Y | str | 最低价 |
  | Close | Y | str | 收盘价 |
  | Volume | Y | str | 成交量 |
  | Amount | Y | str | 成交额 |
  | ForwardFactor | Y | str | 前复权因子，当dividend_type=none时候返回有效值 |
  | VolInStock | N | str | 持仓量，期货数据时Amount为0，非期货数据时VolInStock为0 |
- **使用示例**：
  ```python
  # 获取688318.SH从2025-12-20到今为止最新一条日K线的不复权数据
  df = tq.get_market_data(
          field_list=[],
          stock_list=['688318.SH'],
          start_time='20251220',
          end_time='',
          count=1,
          dividend_type='none',
          period='1d',
          fill_data=True
      )
  ```
- **注意事项**：
  - 一次最多返回24000条数据，要获取完整分钟线需要多次分批获取
  - 返回复权数据时，若该组数据时间内未发生权息变动，则复权价与未复权价相同
  - 后复权数据与取的数据个数有关，只在返回的数据中进行后复权

#### tq.get_market_snapshot(stock_list, field_list=[])
- **功能**：获取实时行情快照
- **参数**：
  - `stock_list`：股票代码列表
  - `field_list`：返回字段列表，默认返回全部字段
- **返回值**：实时行情数据

### 5.3 股票信息与分类数据

#### tq.get_stock_list(market_type=0, list_type=0)
- **功能**：获取系统分类成份股，默认为全部A股
- **参数**：
  - `market_type`：市场类型，详细取值如下：
    | 值 | 含义 |
    |-----|------|
    | 0 | 自选股 |
    | 1 | 持仓股 |
    | 5 | 所有A股 |
    | 6 | 上证指数成份股 |
    | 7 | 上证主板 |
    | 8 | 深证主板 |
    | 9 | 重点指数 |
    | 10 | 所有板块指数 |
    | 11 | 缺省行业板块 |
    | 12 | 概念板块 |
    | 13 | 风格板块 |
    | 14 | 地区板块 |
    | 15 | 缺省行业分类+概念板块 |
    | 16 | 研究行业一级 |
    | 17 | 研究行业二级 |
    | 18 | 研究行业三级 |
    | 21 | 含H股 |
    | 22 | 含可转债 |
    | 23 | 沪深300 |
    | 24 | 中证500 |
    | 25 | 中证1000 |
    | 26 | 国证2000 |
    | 27 | 中证2000 |
    | 28 | 中证A500 |
    | 30 | REITs |
    | 31 | ETF基金 |
    | 32 | 可转债 |
    | 33 | LOF基金 |
    | 34 | 所有可交易基金 |
    | 35 | 所有沪深基金 |
    | 36 | T+0基金 |
    | 49 | 金融类企业 |
    | 50 | 沪深A股 |
    | 51 | 创业板 |
    | 52 | 科创板 |
    | 53 | 北交所 |
    | 101 | 国内期货 |
    | 102 | 港股 |
    | 103 | 美股 |
    | 91 | ETF追踪的指数 |
    | 92 | 国内期货主力合约 |
  - `list_type`：返回类型，0返回代码，1返回代码和名称
- **返回值**：
  - 当list_type=0时，返回股票代码列表，如：['881001.SH', '881006.SH', ...]
  - 当list_type=1时，返回代码名称对列表，如：[{'Code': '881001.SH', 'Name': '煤炭'}, ...]
- **行业板块代码示例**：
  | 代码 | 名称 |
  |------|------|
  | 881001.SH | 煤炭 |
  | 881006.SH | 石油 |
  | 881015.SH | 化工 |
  | 881061.SH | 钢铁 |
  | 881070.SH | 有色 |
  | 881090.SH | 建材 |
  | 881105.SH | 农林牧渔 |
  | 881129.SH | 食品饮料 |
  | 881150.SH | 纺织服饰 |
  | 881166.SH | 轻工制造 |
  | 881183.SH | 家电 |
  | 881199.SH | 商贸 |
  | 881211.SH | 汽车 |
  | 881230.SH | 医药医疗 |
  | 881260.SH | 电力设备 |
  | 881286.SH | 国防军工 |
  | 881292.SH | 机械设备 |
  | 881318.SH | 电子 |
  | 881337.SH | 通信 |
  | 881351.SH | 计算机 |
  | 881368.SH | 传媒 |
  | 881385.SH | 银行 |
  | 881393.SH | 非银金融 |
  | 881405.SH | 建筑 |
  | 881417.SH | 房地产 |
  | 881426.SH | 社会服务 |
  | 881441.SH | 交通运输 |
  | 881458.SH | 公用事业 |
  | 881469.SH | 环保 |
  | 881477.SH | 综合 |

#### tq.get_sector_list(block_type=1, list_type=0)
- **功能**：获取板块列表
- **参数**：
  - `block_type`：板块类型，1表示行业板块，2表示概念板块
  - `list_type`：返回类型，0返回代码，1返回代码和名称
- **返回值**：板块代码列表或代码名称对

#### tq.get_stock_list_in_sector(sector_code, block_type=1, list_type=0)
- **功能**：获取板块内股票列表
- **参数**：
  - `sector_code`：板块代码
  - `block_type`：板块类型，1表示行业板块，2表示期货板块
  - `list_type`：返回类型，0返回代码，1返回代码和名称
- **返回值**：股票代码列表或代码名称对
- **注意事项**：访问空的自定义板块会返回空集而不是报错

#### tq.get_relation(stock_code)
- **功能**：获取股票所属板块
- **参数**：
  - `stock_code`：股票代码
- **返回值**：股票所属板块信息

### 5.3.1 板块纵比功能

**功能概述**：
板块纵比是专用的板块数据纵向比较工具，可以非常方便的对多个板块的指标进行纵向比较。

**可选指标分类**：
- **板块财务数据**：利润表、资产负债表、现金流量表、利润表(单季度)、现金流量表
- **板块财务分析**：每股指标、盈利能力、收益质量、现金流量、资本结构、偿债能力、营运能力、成长能力、单季度指标

**操作步骤**：
1. 设置板块范围
2. 选择指标
3. 设置时间参数及样式选择
4. 提取数据
5. 可将当前的设置存为模板，供重复使用

**使用场景**：
- 比较不同板块的财务表现
- 分析板块间的财务指标差异
- 发现板块轮动机会
- 评估板块投资价值

### 5.4 基本面数据

#### tq.get_stock_info(stock_code, field_list=[])
- **功能**：根据股票，获取股票基础的财务数据
- **参数**：
  - `stock_code`：股票代码
  - `field_list`：字段筛选，传空则返回全部
- **返回值**：包含股票基本信息和财务数据的字典
- **返回字段说明**：
  | 字段 | 说明 |
  |------|------|
  | Name | 股票名称 |
  | Unit | 交易单位 |
  | VolBase | 成交量基数 |
  | MinPrice | 最小价格变动 |
  | ActiveCapital | 流通股本 |
  | J_zgb | 总股本 |
  | J_zzc | 总资产 |
  | J_ldzc | 流动资产 |
  | J_gdzc | 固定资产 |
  | J_wxzc | 无形资产 |
  | J_ldfz | 流动负债 |
  | J_cqfz | 长期负债 |
  | J_jzc | 净资产 |
  | J_yysy | 营业收入 |
  | J_yycb | 营业成本 |
  | J_lyze | 净利润 |
  | J_shly | 税后利润 |
  | J_jly | 净利润 |
  | J_mgsy | 每股收益 |
  | J_mggjj | 每股公积金 |
  | J_mgjzc | 每股净资产 |
  | tdx_dycode | 通达信地域代码 |
  | tdx_dyname | 通达信地域名称 |
  | rs_hycode_sim | 行业代码 |
  | rs_hyname | 行业名称 |
  | blockzscode | 所属的行业板块指数代码 |
- **使用示例**：
  ```python
  fdc = tq.get_stock_info(stock_code='688318.SH', field_list=[])
  ```

#### tq.get_financial_data_by_date(stock_list, field_list, year, mmdd)
- **功能**：根据股票，获取指定日期的专业财务数据，与基础财务数据不同，需要先在客户端中下载专业财务数据
- **参数**：
  - `stock_list`：证券代码列表（必填）
  - `field_list`：字段筛选，不能为空（如 FN193）（必填）
  - `year`：指定年份
  - `mmdd`：指定月日
- **参数说明**：
  - 如果year和mmdd都为0,表示最新的财报
  - 如果year为0,mmdd为小于300的数字,表示最近一期向前推mmdd期的数据,如果是331,630,930,1231这些,表示最近一期的对应季报的数据
  - 如果mmdd为0,year为一数字,表示最近一期向前推year年的同期数据
  - 季报分界点为:0331,0630,0930,1231
- **返回值**：包含专业财务数据的字典
- **使用示例**：
  ```python
  fd = tq.get_financial_data_by_date(
          stock_list=['688318.SH'],
          field_list=['Fn193','Fn194','Fn195','Fn196','Fn197'],
          year=0,
          mmdd=0)
  ```
- **注意事项**：需要先在客户端中下载财务数据包

#### tq.get_more_info(stock_list, field_list=[])
- **功能**：获取股票更多信息
- **参数**：
  - `stock_list`：股票代码列表
  - `field_list`：返回字段列表，默认返回全部字段
- **返回值**：股票更多信息

#### tq.get_gb_info(stock_code, start_date, end_date)
- **功能**：获取每天的股本数据
- **参数**：
  - `stock_code`：股票代码
  - `start_date`：开始日期
  - `end_date`：结束日期
- **返回值**：股本数据

#### tq.get_kzz_info(stock_list)
- **功能**：获取可转债信息
- **参数**：
  - `stock_list`：股票代码列表
- **返回值**：可转债信息

#### tq.get_trackzs_etf_info()
- **功能**：获取跟踪指数的ETF信息
- **参数**：无
- **返回值**：ETF信息

### 5.5 通达信公式调用

#### tq.formula_format_data(stock_code, period, start_date, end_date, count)
- **功能**：格式化K线数据
- **参数**：
  - `stock_code`：股票代码
  - `period`：K线周期
  - `start_date`：开始日期
  - `end_date`：结束日期
  - `count`：数据量
- **返回值**：格式化后的K线数据

#### tq.formula_set_data(data_type, data)
- **功能**：向通达信公式系统设置数据
- **参数**：
  - `data_type`：数据类型
  - `data`：数据
- **返回值**：设置结果

#### tq.formula_set_data_info(data_info)
- **功能**：向通达信公式系统设置数据信息
- **参数**：
  - `data_info`：数据信息
- **返回值**：设置结果

#### tq.formula_get_data(data_type)
- **功能**：获取公式中的设置数据
- **参数**：
  - `data_type`：数据类型
- **返回值**：数据

#### tq.formula_zb(stock_code, formula_name, params, xsflag=2)
- **功能**：调用通达信技术指标公式
- **参数**：
  - `stock_code`：股票代码
  - `formula_name`：公式名称
  - `params`：公式参数
  - `xsflag`：小数位数设置
- **返回值**：指标计算结果

#### tq.formula_xg(stock_code, formula_name, params)
- **功能**：调用通达信条件选股公式
- **参数**：
  - `stock_code`：股票代码
  - `formula_name`：公式名称
  - `params`：公式参数
- **返回值**：选股结果

#### tq.formula_exp(stock_code, formula_name, params)
- **功能**：调用通达信专家系统公式
- **参数**：
  - `stock_code`：股票代码
  - `formula_name`：公式名称
  - `params`：公式参数
- **返回值**：专家系统结果

#### tq.formula_process_mul_xg(stock_list, formula_name, params)
- **功能**：批量调用选股公式
- **参数**：
  - `stock_list`：股票代码列表
  - `formula_name`：公式名称
  - `params`：公式参数
- **返回值**：批量选股结果

#### tq.formula_process_mul_zb(stock_list, formula_name, params, retrun_count=1)
- **功能**：批量调用指标公式
- **参数**：
  - `stock_list`：股票代码列表
  - `formula_name`：公式名称
  - `params`：公式参数
  - `retrun_count`：返回数量
- **返回值**：批量指标计算结果

### 5.6 交易功能

#### tq.stock_account()
- **功能**：获取资金账户句柄
- **参数**：无
- **返回值**：账户句柄

#### tq.query_stock_asset()
- **功能**：账户资产查询
- **参数**：无
- **返回值**：账户资产信息

#### tq.query_stock_orders()
- **功能**：查询账户委托信息
- **参数**：无
- **返回值**：委托信息

#### tq.query_stock_positions()
- **功能**：查询账户持仓信息
- **参数**：无
- **返回值**：持仓信息

#### tq.order_stock(stock_code, price, volume, direction, order_type=0, account_type=0)
- **功能**：交易执行函数
- **参数**：
  - `stock_code`：股票代码
  - `price`：价格
  - `volume`：数量
  - `direction`：方向，0买入，1卖出
  - `order_type`：订单类型
  - `account_type`：账户类型，0普通账户，1信用账户
- **返回值**：下单结果

#### tq.cancel_order_stock(order_id)
- **功能**：撤单
- **参数**：
  - `order_id`：订单ID
- **返回值**：撤单结果

### 5.7 预警与回测

#### tq.send_warn(stock_list, time_list, price_list, close_list, volum_list, bs_flag_list, warn_type_list, reason_list, count)
- **功能**：发送预警信号给通达信客户端的TQ策略界面
- **参数**：
  - `stock_list`：股票代码列表
  - `time_list`：时间列表
  - `price_list`：价格列表
  - `close_list`：收盘价列表
  - `volum_list`：成交量列表
  - `bs_flag_list`：买卖标志列表，0买1卖2未知
  - `warn_type_list`：预警类型列表
  - `reason_list`：预警原因列表
  - `count`：数量
- **返回值**：发送结果

#### tq.send_bt_data(stock_code, time_list, data_list, count)
- **功能**：发送回测数据到TQ
- **参数**：
  - `stock_code`：股票代码
  - `time_list`：时间列表
  - `data_list`：数据列表
  - `count`：数量
- **返回值**：发送结果

### 5.8 其他功能

#### tq.send_file(file_path)
- **功能**：发送文件到通达信客户端
- **参数**：
  - `file_path`：文件路径
- **返回值**：发送结果

#### tq.download_file(file_type, date)
- **功能**：下载文件，支持最近舆情、综合信息文件
- **参数**：
  - `file_type`：文件类型
  - `date`：日期
- **返回值**：下载结果

#### tq.send_user_block(block_name, stock_list)
- **功能**：发送用户板块，可添加股票进自选股（自选股简称为ZXG）
- **参数**：
  - `block_name`：板块名称
  - `stock_list`：股票代码列表
- **返回值**：发送结果

#### tq.exec_to_tdx(func_name, params)
- **功能**：调用客户端功能接口
- **参数**：
  - `func_name`：函数名称
  - `params`：参数
- **返回值**：调用结果

## 6. 版本更新日志

### 2026-03-27 更新
- 新增函数：获取股票所属板块get_relation
- 新增函数：调用客户端功能接口exec_to_tdx
- 新增函数：撤单cancel_order_stock
- 新增函数：账户资产查询query_stock_asset
- 更新函数：交易类账户函数逻辑更新
- 更新函数：order_stock对于模拟账户自动下单
- 更新函数：order_stock新增信用交易：担保品买入、担保品卖出，融资买入，融券卖出
- 更新函数：get_stock_list_in_sector访问空的自定义板块会返回空集而不是报错
- 问题修复：修复了get_market_data、refresh_kline等函数无法处理期权的问题
- 其他更新：期货期权类型支持，新增相关宏定义（常量枚举）

### 2026-03-20 更新
- 新增函数：获取资金账户句柄stock_account
- 新增函数：查询账户委托信息query_stock_orders
- 新增函数：查询账户持仓信息query_stock_positions
- 新增函数：交易执行函数order_stock
- 更新函数：get_stock_list_in_sector新增block_type=2，可取对应期货代码
- 更新函数：get_more_info新增字段QHMainYYMM
- 更新函数：get_stock_list新增参数92: 国内期货主力合约
- 更新函数：get_cb_info改名为get_kzz_info

### 2026-03-06 更新
- 新增函数：获取跟踪指数的ETF信息get_trackzs_etf_info
- 更新函数：refresh_cache新增参数 'ZS' 表示沪深京指数
- 更新函数：get_stock_list新增参数91 跟踪指数的ETF信息
- 其他修正：未识别的市场后缀由默认的SZ改为OT
- 其他修正：修复get_market_data某些情况下会报NoneType的bug

### 2026-02-28 更新
- 问题修复：修复了formula_process_mul_zb等入参retrun_count拼写错误问题
- 更新函数：get_more_info，get_cb_info，get_market_snapshot加上了字段筛选功能
- 更新函数：get_more_info等支持更多行情数据项，输出顺序进行归整
- 其他修正：tqcenter几处细节修改

### 2026-02-12 更新
- 更新函数：send_user_block可以添加股票进自选股，自选股简称为ZXG
- 其他更新：批量调用公式内部优化提速
- 其他更新：新增港股指数（.HI）
- 其他更新：解决多个客户端同时运行时的TQ冲突的问题

### 2026-02-07 更新
- 新增函数：批量调用选股公式formula_process_mul_xg
- 新增函数：批量调用指标公式formula_process_mul_zb
- 更新函数：get_stock_list、 get_sector_list、 get_stock_list_in_sector新增参数list_type，可以选择返回股票名称
- 更新函数：tdx_formula返回做出修改，条件选股和专家选股只返回'1'和'0'
- 更新函数：formula_zb新增参数xsflag，可以设置返回数据的小数位数
- 更新函数：download_file新增下载：最近舆情、综合信息文件
- 更新函数：get_stock_info新增部分数据字段输出

### 2026-01-31 更新
- 新增功能：支持调用通达信公式进行计算
- 新增函数：格式化K线数据formula_format_data
- 新增函数：向通达信公式系统设置数据formula_set_data
- 新增函数：向通达信公式系统设置数据信息formula_set_data_info
- 新增函数：获取公式中的设置数据formula_get_data
- 新增函数：调用通达信技术指标公式formula_zb
- 新增函数：调用通达信条件选股公式formula_xg
- 新增函数：调用通达信专家系统公式formula_exp
- 新增函数：获取股票更多信息get_more_info
- 新增函数：获取每天的股本数据get_gb_info
- 更新函数：刷新行情缓存refresh_cache，新增参数force和market，可指定强制刷新或指定市场刷新
- 其他更新：新增中证指数（.CSI），中金所期货（.CFF），宏观数据（.HG）等市场后缀识别和数据获取
- 其他更新：获取非指定日期的股票交易数据，板块交易数据等数据时增加了对应日期返回
- 问题修复：修复了部分市场数据返回时小数位数不对导致的精度问题
- 问题修复：修复了获取Python3.9以及之前版本依赖库错误问题

### 2026-01-17 正式发布

## 7. 使用示例

### 7.1 基本初始化与数据获取

```python
import numpy as np
import pandas as pd
from tqcenter import tq
import time
import json

# 初始化
tq.initialize(__file__) #所有策略连接通达信客户端都必须调用此函数进行初始化

# 刷新行情缓存
refresh_cache = tq.refresh_cache()
print(refresh_cache)

# 缓存历史K线
refresh_kline = tq.refresh_kline(stock_list=['688318.SH'],period='1d')
print(refresh_kline)

# 获取K线数据
df = tq.get_market_data(
        field_list=[],
        stock_list=['600519.SH'],
        start_time='20251208',
        end_time='20251210',
        count=-1,
        dividend_type='none',
        period='1d',
        fill_data=False
    )
print(df)
```

### 7.2 发送预警信号

```python
# 发送预警信号给通达信客户端的TQ策略界面
warn_res = tq.send_warn(stock_list = ['688318.SH','688318.SH','600519.SH','600519.SH'],
             time_list = ['20251215141115','20251215142100','20251215143101','20251215145001'],
             price_list= ['123.45','133.45','1823.45','1853.45'],
             close_list= ['122.50','132.50','1822.50','1822.50'],
             volum_list= ['1000','2000','15000','15000'],
             bs_flag_list= ['0','','2','1'],
             warn_type_list= ['0','','2','1'],
             reason_list= ['价格突破预警线','收盘价突破预警线','成交量突破预警线','价格下破预警线'],
             count=4)
print(warn_res)
```

### 7.3 发送回测数据

```python
# 发送回测数据
time_list = ['20260120141100','20260120141400']
data_list = [['1','143.41','200','0','0','0'],['0','0','0','1','143.48','200']]
bt_data = tq.send_bt_data(stock_code = '688318.SH',
                          time_list = time_list,
                          data_list = data_list,
                          count = 2)
print(bt_data)
```

### 7.4 完整策略示例

```python
# 完整的策略示例，包含技术指标计算、回测和结果展示
from tqcenter import tq
import pandas as pd
import numpy as np
import math
from datetime import datetime
import sys

# 初始化
tq.initialize(__file__)

# 股票列表（示例）
stocks = ['688800.SH', '688318.SH', '688981.SH']
# 基准品种代码
benchmark_code = '000300.SH'

# 技术指标计算函数
def calculate_ma(series, window):
    """计算简单移动平均"""
    return series.rolling(window=window).mean()

def calculate_cross_signal(fast_series, slow_series):
    """计算金叉信号序列"""
    cross_up = (fast_series > slow_series) & (fast_series.shift(1) <= slow_series.shift(1))
    return cross_up.astype(int)

# 主程序
for stock_code in stocks:
    print(f"处理股票: {stock_code}")
    
    # 获取股票市场数据
    market_data = tq.get_market_data(
        field_list=['Open', 'High', 'Low', 'Close'],
        stock_list=[stock_code],
        period='1d',
        count=60,
        dividend_type='front'  # 前复权数据
    )

    # 构建DataFrame
    df = pd.DataFrame({
        'open': market_data['Open'][stock_code],
        'high': market_data['High'][stock_code],
        'low': market_data['Low'][stock_code],
        'close': market_data['Close'][stock_code]
    })

    # 计算技术指标
    df['ma5'] = calculate_ma(df['close'], 5)
    df['ma10'] = calculate_ma(df['close'], 10)

    # 计算金叉信号
    df['buyxh'] = calculate_cross_signal(df['ma5'], df['ma10'])
    df['sellxh'] = calculate_cross_signal(df['ma10'], df['ma5'])

    # 准备发送给TQ的数据
    time_list = df.index.strftime('%Y%m%d').tolist()
    data_list = []
    for i, (_, row) in enumerate(df.iterrows()):
        ma5_value = row['ma5'] if not pd.isna(row['ma5']) else 0.0
        ma10_value = row['ma10'] if not pd.isna(row['ma10']) else 0.0
        buyxh_value = row['buyxh'] if not pd.isna(row['buyxh']) else 0
        sellxh_value = row['sellxh'] if not pd.isna(row['sellxh']) else 0
        
        formatted_entry = [
            f"{ma5_value:.2f}",                    # ID 1: MA5
            f"{ma10_value:.2f}",                   # ID 2: MA10
            str(int(buyxh_value)),                 # ID 3: 买入信号
            str(int(sellxh_value)),                # ID 4: 卖出信号
        ]
        data_list.append(formatted_entry)

    # 发送回测数据到TQ
    bt_data = tq.send_bt_data(
        stock_code,
        time_list=time_list,
        data_list=data_list,
        count=60
    )
    print("发送回测数据结果:")
    print(bt_data)

# 关闭TQ连接
tq.close()
print("所有股票处理完毕！")
```

## 8. 通达信公式使用提示

将数据发送到TQ策略界面后，您可以在通达信公式管理器中创建技术指标公式，使用 SIGNALS_TQ(ID, TYPE) 函数来引用这些序列数据并在K线上展示。

例如，创建一个名为"TQMA510"的公式，代码可以如下：

```
MA5:SIGNALS_TQ(1,0);        {引用ID=1的数据(MA5)}
MA10:SIGNALS_TQ(2,0);       {引用ID=2的数据(MA10)}

{交易信号}
BUY_SIGNAL:=SIGNALS_TQ(3,0); {买入信号}
SELL_SIGNAL:=SIGNALS_TQ(4,0);{卖出信号}

{绘制交易信号图标}
DRAWICON(BUY_SIGNAL, LOW, 1);
DRAWICON(SELL_SIGNAL, HIGH, 2);
```

函数说明：
```
SIGNALS_TQ(ID, TYPE)
    ID: TQ数据中的序号 (1-16)，对应data_list子列表中的位置。
    TYPE: 处理方式。
        1 - 平滑处理，没有自定义数据的周期返回上一周期的值。
        0 - 不做平滑处理。
        2 - 没有数据则为0。
```

## 9. 注意事项

1. **数据准备**：获取K线数据需要先在客户端中下载对应盘后数据，调用会触发客户端刷新数据，耗时过长请耐心等待。

2. **缓存机制**：刷新行情缓存后5分钟内取最新report和k线数据不会触发刷新，可有效提高数据获取速度。

3. **数据格式**：
   - 股票代码格式必须是标准格式：6位数+市场后缀（.SH/.SZ/.JJ等）
   - 时间格式必须是：YYYYMMDD 或 YYYYMMDDHHMMSS
   - K线周期：1d/1w/1m/5m/15m/30m/60m等
   - 复权类型：none-不复权，front-前复权，back-后复权

4. **性能优化**：
   - 缓存历史K线时，不建议一次更新太多，会堵塞策略和客户端
   - 批量调用公式时，内部已做优化提速

5. **错误处理**：
   - get_stock_list_in_sector访问空的自定义板块会返回空集而不是报错
   - 修复了get_market_data某些情况下会报NoneType的bug

6. **多客户端**：解决了多个客户端同时运行时的TQ冲突的问题

7. **市场支持**：
   - 新增港股指数（.HI）
   - 新增中证指数（.CSI），中金所期货（.CFF），宏观数据（.HG）等市场后缀识别和数据获取
   - 支持期货期权类型

## 10. 常见问题

### 10.1 连接问题
- **问题**：无法连接通达信客户端
- **解决方案**：确保已启动支持TQ策略功能的通达信金融终端、量化模拟版或专业研究版，并已登录

### 10.2 数据问题
- **问题**：获取K线数据为空
- **解决方案**：在通达信客户端中下载对应盘后数据，然后再调用get_market_data

### 10.3 权限问题
- **问题**：无法调用交易相关函数
- **解决方案**：确保使用的通达信版本支持交易功能，并且已登录交易账户

### 10.4 性能问题
- **问题**：数据获取速度慢
- **解决方案**：合理使用refresh_cache和refresh_kline函数，减少不必要的数据刷新

## 11. 总结

TdxQuant是一款集金融数据与策略投研工具于一体的量化平台，结构清晰，简洁易上手，数据获取快捷，算法资源丰富。平台提供了从策略研发、回测到实盘交易的完整解决方案，大幅降低了量化交易的门槛。

通过本文档，您可以了解TdxQuant的核心功能、API接口使用方法以及最佳实践，为您的量化投资之路提供有力支持。