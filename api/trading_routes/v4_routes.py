"""从 api/trading.py 拆出的 v4_routes 路由模块。"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
import api.trading as _host
globals().update({k: v for k, v in vars(_host).items() if not k.startswith('__') and k != 'router'})

router = APIRouter()

@router.get("/v4/market-gate")
def get_v4_market_gate(
    trade_date: Optional[str] = Query(None, description="open gate trade date"),
):
    target_date = _normalize_date_str(trade_date) if isinstance(trade_date, (str, date, datetime)) else None
    params: List[Any] = ["999999.SH"]
    date_filter = ""
    if target_date:
        date_filter = "AND k.trade_date <= ?::DATE"
        params.append(target_date)
    try:
        df = clickhouse_query_df(
            f"""
            SELECT code, name, trade_date, close
            FROM (
                SELECT
                    k.code,
                    s.name,
                    k.trade_date,
                    k.close,
                    row_number() OVER (
                        PARTITION BY k.code, k.trade_date
                        ORDER BY ifNull(k.created_at, toDateTime('1970-01-01')) DESC, k.id DESC
                    ) AS rn
                FROM kline_daily k
                LEFT JOIN stocks s ON s.code = k.code
                WHERE k.code = ?
                  {date_filter}
            )
            WHERE rn = 1
            ORDER BY trade_date DESC
            LIMIT 30
            """,
            params,
        )
    except Exception as exc:
        logger.warning(f"load v4 market gate from ClickHouse failed: trade_date={target_date or ''}, error={exc}")
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数数据",
                "message": "指数数据暂时不可用，已降级为不可开仓。",
            }
        )
    if df is None or df.empty:
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数数据",
                "message": "指数数据不存在或暂时不可用。",
            }
        )
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date").reset_index(drop=True)
    snapshot_date = target_date or _normalize_date_str(_resolve_latest_stock_trade_date())
    live_snapshot: Dict[str, Any] = {}
    if snapshot_date:
        try:
            if not clickhouse_table_exists("intraday_quote_snapshot"):
                snap_df = pd.DataFrame()
            else:
                snap_df = clickhouse_query_df(
                    """
                    SELECT code, name, snapshot_time, price
                    FROM intraday_quote_snapshot
                    WHERE code IN ('999999.SH', '000001.SH')
                      AND asset_type = 'index'
                      AND snapshot_date = ?::DATE
                      AND price > 0
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    [snapshot_date],
                )
            if snap_df is not None and not snap_df.empty:
                snap = snap_df.iloc[0]
                live_snapshot = {
                    "code": str(snap.get("code") or "999999.SH"),
                    "name": str(snap.get("name") or "上证指数"),
                    "snapshot_time": str(snap.get("snapshot_time") or ""),
                    "price": _to_float(snap.get("price")),
                }
        except Exception as exc:
            logger.warning(f"load shanghai intraday market gate snapshot failed: {exc}")
    if live_snapshot.get("price") is not None:
        snap_day = pd.to_datetime(snapshot_date, errors="coerce")
        prev_df = df[df["trade_date"] < snap_day].copy() if pd.notna(snap_day) else pd.DataFrame()
        if len(prev_df) >= 19:
            close = _to_float(live_snapshot.get("price"))
            ma20 = _to_float((prev_df["close"].tail(19).sum() + float(close or 0.0)) / 20.0)
            can_open = bool(close is not None and ma20 is not None and close >= ma20)
            name = str(live_snapshot.get("name") or "指数")
            message = (
                f"{name} 现报 {close:.2f} >= MA20 {ma20:.2f}，判定可开启。"
                if can_open
                else f"{name} 现报 {close:.2f} < MA20 {ma20:.2f}，不满足开仓条件，建议延后。"
            )
            return _sanitize(
                {
                    "available": True,
                    "can_open": can_open,
                    "code": "999999.SH",
                    "name": name,
                    "trade_date": snapshot_date,
                    "close": close,
                    "ma20": ma20,
                    "source": "intraday_quote_snapshot",
                    "snapshot_time": live_snapshot.get("snapshot_time"),
                    "daily_base_count": 19,
                    "rule": "指数现报 >= MA20 且此前至少有19个交易日历史可用于计算。",
                    "message": message,
                }
            )
    if len(df) < 20:
        return _sanitize(
            {
                "available": False,
                "can_open": False,
                "code": "999999.SH",
                "name": "指数",
                "message": "指数 MA20 所需历史数据不足，暂时无法计算。",
            }
        )
    latest = df.iloc[-1]
    ma20 = _to_float(df["close"].tail(20).mean())
    close = _to_float(latest.get("close"))
    can_open = bool(close is not None and ma20 is not None and close >= ma20)
    trade_date_text = str(latest.get("trade_date").date())
    name = str(latest.get("name") or "指数")
    message = (
        f"{name} 收盘 {close:.2f} >= MA20 {ma20:.2f}，判定可开启。"
        if can_open
        else f"{name} 收盘 {close:.2f} < MA20 {ma20:.2f}，不满足开仓条件，建议延后。"
    )
    return _sanitize(
        {
            "available": True,
            "can_open": can_open,
            "code": "999999.SH",
            "name": name,
            "trade_date": trade_date_text,
            "close": close,
            "ma20": ma20,
            "rule": "指数收盘 >= MA20 且有足够历史样本可用于计算。",
            "message": message,
        }
    )


@router.post("/v4/manual-holdings/signals")
def get_v4_manual_holdings_signals(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    result_rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()[:6]
        if not code:
            continue
        name = str(item.get("name") or code)
        period15_df = _load_minute_bars_for_signal(code, 15, limit=240)
        period30_df = _load_minute_bars_for_signal(code, 30, limit=240)
        signal15 = _detect_bearish_divergence(period15_df) if not period15_df.empty else {"detected": False, "reason": "15m数据不足，暂不评估。"}
        signal30 = _detect_bearish_divergence(period30_df) if not period30_df.empty else {"detected": False, "reason": "30m数据不足，暂不评估。"}
        rsi_box_t = _detect_rsi_box_t_signal(period15_df) if not period15_df.empty else {
            "enabled": False,
            "status": "data_missing",
            "action": "observe",
            "recommendation": "15m数据不足，暂不评估箱体做T。",
        }
        risk_level = "low"
        if signal15.get("detected") and signal30.get("detected"):
            risk_level = "high"
        elif signal15.get("detected") or signal30.get("detected"):
            risk_level = "medium"
        suggestion = "观察中"
        if risk_level == "high":
            suggestion = "15m与30m均示弱，建议控制仓位并优先观察。"
        elif risk_level == "medium":
            suggestion = "短时出现回落信号，建议谨慎加码。"
        result_rows.append(
            {
                "code": code,
                "name": name,
                "rsi15_signal": signal15,
                "rsi30_signal": signal30,
                "rsi_box_t": rsi_box_t,
                "risk_level": risk_level,
                "suggestion": suggestion,
            }
        )
    return _sanitize({"rows": result_rows, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@router.post("/v4/manual-holdings/quotes")
def get_v4_manual_holdings_quotes(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code6 = str(item.get("code") or "").strip()[:6]
        if not code6:
            continue
        cost_price = _to_float(item.get("cost_price"))
        latest_price = None
        prev_close = None
        latest_date = None
        if clickhouse_available():
            try:
                qdf = clickhouse_query_df(
                    """
                    SELECT code, trade_date, close
                    FROM kline_daily
                    WHERE substr(code, 1, 6) = ?
                    ORDER BY trade_date DESC
                    LIMIT 2
                    """,
                    [code6],
                )
                if qdf is not None and not qdf.empty:
                    latest_price = _to_float(qdf.iloc[0].get("close"))
                    latest_date = str(qdf.iloc[0].get("trade_date") or "")
                    if len(qdf) >= 2:
                        prev_close = _to_float(qdf.iloc[1].get("close"))
            except Exception as exc:
                logger.warning(f"load latest quote from ClickHouse failed: code={code6}, error={exc}")
        day_pnl = None
        shares = _to_float(item.get("shares"))
        if latest_price is not None and prev_close is not None and shares is not None:
            day_pnl = (latest_price - prev_close) * shares
        pnl_ratio = None
        if latest_price is not None and cost_price not in (None, 0):
            pnl_ratio = (latest_price / cost_price - 1.0) * 100.0
        rows.append(
            {
                "code": code6,
                "latest_price": latest_price,
                "prev_close": prev_close,
                "latest_trade_date": latest_date,
                "day_pnl": round(float(day_pnl), 2) if day_pnl is not None else None,
                "pnl_ratio": round(float(pnl_ratio), 4) if pnl_ratio is not None else None,
            }
        )
    return _sanitize({"rows": rows, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@router.post("/v4/manual-holdings/refresh-all")
def refresh_v4_manual_holdings_all(payload: Dict[str, Any]):
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    signal_date = _normalize_date_str(payload.get("signal_date")) if isinstance(payload, dict) else None
    if not signal_date:
        signal_date = _normalize_date_str(payload.get("target_date")) if isinstance(payload, dict) else None
    if not signal_date:
        signal_date = _normalize_date_str(_resolve_latest_stock_trade_date())

    # 1) latest quote + pnl
    quote_resp = get_v4_manual_holdings_quotes({"holdings": holdings})
    quote_map = {
        str(item.get("code") or "").strip()[:6]: item
        for item in (quote_resp.get("rows") or [])
        if isinstance(item, dict)
    }

    # 2) rsi signals
    signal_resp = get_v4_manual_holdings_signals({"holdings": holdings})
    signal_map = {
        str(item.get("code") or "").strip()[:6]: item
        for item in (signal_resp.get("rows") or [])
        if isinstance(item, dict)
    }

    # 3) v4 score/rank (latest <= signal_date)
    codes = [str(item.get("code") or "").strip()[:6] for item in holdings if isinstance(item, dict)]
    codes = [c for c in codes if c]
    score_map: Dict[str, Dict[str, Any]] = {}
    if codes and signal_date:
        placeholders = ", ".join([f":c{i}" for i in range(len(codes))])
        params: Dict[str, Any] = {
            "strategy_version": str(DEFAULT_LIVE_STRATEGY_VERSION or "").strip() or "v5",
            "signal_date": signal_date,
        }
        for i, code in enumerate(codes):
            params[f"c{i}"] = code
        sql = text(
            f"""
            SELECT t.code, t.signal_date, t.score_rank, t.score_total
            FROM {TRADING_SELECTION_SCORE_TABLE} t
            JOIN (
              SELECT code, MAX(signal_date) AS max_signal_date
              FROM {TRADING_SELECTION_SCORE_TABLE}
              WHERE strategy_version = :strategy_version
                AND signal_date <= :signal_date
                AND code IN ({placeholders})
              GROUP BY code
            ) x ON x.code = t.code AND x.max_signal_date = t.signal_date
            WHERE t.strategy_version = :strategy_version
            """
        )
        try:
            with db.engine.connect() as conn:
                df = pd.read_sql(sql, conn, params=params)
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    c = str(row.get("code") or "").strip()[:6]
                    if not c:
                        continue
                    score_map[c] = {
                        "v4_signal_date": str(row.get("signal_date") or ""),
                        "v4_rank": _to_int(row.get("score_rank"), 0),
                        "v4_score": _to_float(row.get("score_total")),
                    }
        except Exception as exc:
            logger.warning(f"load v4 scores for manual holdings failed: {exc}")

    # 3.1) fallback: if the score table misses any current holding, recompute V4 daily pool on signal_date
    missing_score_codes = [code for code in codes if code not in score_map]
    if missing_score_codes and signal_date:
        try:
            hist = _load_hist_with_factors(signal_date)
            if hist is not None and not hist.empty:
                day_df = hist[hist["date"] == pd.Timestamp(signal_date)].copy()
                scored_df = _score_candidates_on_signal_day(day_df)
                if scored_df is not None and not scored_df.empty:
                    scored_df["code"] = scored_df["code"].astype(str).str[:6]
                    for _, row in scored_df.iterrows():
                        c = str(row.get("code") or "").strip()[:6]
                        if not c or c not in missing_score_codes:
                            continue
                        score_map[c] = {
                            "v4_signal_date": signal_date,
                            "v4_rank": _to_int(row.get("score_rank"), 0),
                            "v4_score": _to_float(row.get("score")),
                        }
        except Exception as exc:
            logger.warning(f"fallback recompute v4 score map failed: signal_date={signal_date}, error={exc}")

    merged_rows: List[Dict[str, Any]] = []
    for item in holdings:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()[:6]
        if not code:
            continue
        quote_item = quote_map.get(code, {})
        signal_item = signal_map.get(code, {})
        score_item = score_map.get(code, {})
        in_score_pool = bool(score_item) and _to_int(score_item.get("v4_rank"), 0) > 0
        score_pool_status = "in_pool" if in_score_pool else "out_of_pool"
        risk_level = str(signal_item.get("risk_level") or "low")
        suggestion = str(signal_item.get("suggestion") or "继续观察")
        if not in_score_pool:
            risk_level = "high"
            suggestion = "暂未入池：评分池缺失或异常，建议仅观察不下单。"
        merged_rows.append(
            _sanitize(
                {
                    "code": code,
                    "name": item.get("name"),
                    "latest_price": quote_item.get("latest_price"),
                    "prev_close": quote_item.get("prev_close"),
                    "latest_trade_date": quote_item.get("latest_trade_date"),
                    "day_pnl": quote_item.get("day_pnl"),
                    "pnl_ratio": quote_item.get("pnl_ratio"),
                    "rsi15_signal": signal_item.get("rsi15_signal"),
                    "rsi30_signal": signal_item.get("rsi30_signal"),
                    "rsi_box_t": signal_item.get("rsi_box_t"),
                    "risk_level": risk_level,
                    "suggestion": suggestion,
                    "v4_signal_date": score_item.get("v4_signal_date"),
                    "v4_rank": score_item.get("v4_rank"),
                    "v4_score": score_item.get("v4_score"),
                    "in_score_pool": bool(in_score_pool),
                    "score_pool_status": score_pool_status,
                }
            )
        )
    return _sanitize(
        {
            "signal_date": signal_date,
            "rows": merged_rows,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


@router.post("/v4/manual-holdings/state")
def save_v4_manual_holdings_state(payload: Dict[str, Any]):
    state = _load_v4_monitor_state()
    holdings = payload.get("holdings") if isinstance(payload, dict) else []
    if not isinstance(holdings, list):
        holdings = []
    state["holdings"] = holdings
    state["reminder_active"] = False
    state["reminder_activated_at"] = None
    state["last_alert_by_code"] = {}
    _save_v4_monitor_state(state)
    return _sanitize({"ok": True, "holdings_count": len(holdings)})


@router.get("/v4/manual-holdings/monitor/status")
def get_v4_manual_holdings_monitor_status():
    # R43：GET 只读——现场快照，绝不 configure（零 add/remove_job/start）。
    return _sanitize(_v4_monitor_status_readonly())


@router.post("/v4/manual-holdings/monitor/config")
def set_v4_manual_holdings_monitor_config(payload: Dict[str, Any]):
    state = _load_v4_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(30, _to_int(payload.get("interval_seconds"), 60))
        if "recipient_email" in payload:
            state["recipient_email"] = str(payload.get("recipient_email") or "").strip()
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "quiet_minutes" in payload:
            state["quiet_minutes"] = max(0, _to_int(payload.get("quiet_minutes"), 15))
        if "repeat_reminder_minutes" in payload:
            state["repeat_reminder_minutes"] = max(60, _to_int(payload.get("repeat_reminder_minutes"), 60))
        holdings = payload.get("holdings")
        if isinstance(holdings, list):
            state["holdings"] = holdings
            state["reminder_active"] = False
            state["reminder_activated_at"] = None
            state["last_alert_by_code"] = {}
    _save_v4_monitor_state(state)
    return _sanitize(_configure_v4_monitor_scheduler())


@router.post("/v4/manual-holdings/monitor/run-once")
def run_v4_manual_holdings_monitor_once(payload: Dict[str, Any]):
    recipient_email = None
    force_send = False
    if isinstance(payload, dict):
        txt = str(payload.get("recipient_email") or "").strip()
        recipient_email = txt if txt else None
        force_send = bool(payload.get("force_send"))
    result = _run_v4_manual_holdings_monitor(force_send=force_send, recipient_override=recipient_email)
    return _sanitize(result)


@router.post("/v4/manual-holdings/ths-current-table")
def read_v4_manual_holdings_ths_current_table():
    return _sanitize(_read_ths_current_table_text())


@router.post("/v4/manual-holdings/ths-delivery-file")
def read_v4_manual_holdings_ths_delivery_file():
    return _sanitize(_read_ths_delivery_file_text())


@router.post("/v4/manual-holdings/ths-capital-holdings")
def read_v4_manual_holdings_ths_capital_holdings():
    return _sanitize(_read_ths_capital_holdings_text())


@router.get("/v4/manual-holdings/entry-backtest")
def get_v4_manual_holding_entry_backtest(
    code: str = Query(..., description="6位股票代码"),
    strategy_version: Optional[str] = Query(None, description="策略版本"),
    max_samples: int = Query(400, ge=50, le=3000, description="朢大样本数"),
):
    code6 = str(code or "").strip()[:6]
    if not re.fullmatch(r"\d{6}", code6):
        return _sanitize({"ok": False, "message": "股票代码无效"})

    sv = str(strategy_version or DEFAULT_LIVE_STRATEGY_VERSION or "v5").strip()
    try:
        with db.engine.connect() as conn:
            signal_df = pd.read_sql(
                text(
                    f"""
                    SELECT signal_date, score_total, score_rank
                    FROM {TRADING_SELECTION_SCORE_TABLE}
                    WHERE strategy_version = :sv
                      AND code = :code
                      AND passed_entry_min_score = 1
                    ORDER BY signal_date DESC
                    LIMIT :max_samples
                    """
                ),
                conn,
                params={"sv": sv, "code": code6, "max_samples": int(max_samples)},
            )
            price_df = pd.read_sql(
                text(
                    """
                    SELECT trade_date, close
                    FROM kline_daily
                    WHERE substr(code, 1, 6) = :code
                    ORDER BY trade_date ASC
                    """
                ),
                conn,
                params={"code": code6},
            )
    except Exception as exc:
        logger.warning(f"manual holding entry backtest load failed: code={code6}, error={exc}")
        return _sanitize({"ok": False, "message": f"读取历史数据失败: {exc}"})

    if signal_df is None or signal_df.empty:
        return _sanitize(
            {
                "ok": True,
                "code": code6,
                "strategy_version": sv,
                "sample_count": 0,
                "horizons": {},
                "message": "该股在当前策略版本下暂无历史入场样本",
            }
        )
    if price_df is None or price_df.empty:
        return _sanitize({"ok": False, "message": "缺少日线数据"})

    signal_df = signal_df.copy()
    price_df = price_df.copy()
    signal_df["signal_date"] = pd.to_datetime(signal_df["signal_date"], errors="coerce").dt.date
    price_df["trade_date"] = pd.to_datetime(price_df["trade_date"], errors="coerce").dt.date
    price_df["close"] = pd.to_numeric(price_df["close"], errors="coerce")
    price_df = price_df.dropna(subset=["trade_date", "close"]).reset_index(drop=True)
    if price_df.empty:
        return _sanitize({"ok": False, "message": "行情数据为空，无法计算回测。"})

    closes = price_df["close"].tolist()
    date_to_idx: Dict[Any, int] = {d: i for i, d in enumerate(price_df["trade_date"].tolist())}

    horizons = [3, 5, 10]
    metrics: Dict[str, Dict[str, Any]] = {}
    realized_rows: List[Dict[str, Any]] = []
    valid_sample_count = 0

    for _, row in signal_df.iterrows():
        d = row.get("signal_date")
        idx = date_to_idx.get(d)
        if idx is None:
            continue
        valid_sample_count += 1
        row_item: Dict[str, Any] = {
            "signal_date": str(d),
            "score_total": _to_float(row.get("score_total")),
            "score_rank": _to_int(row.get("score_rank"), 0),
        }
        base = closes[idx]
        for n in horizons:
            key = f"d{n}"
            if idx + n < len(closes) and base and base > 0:
                ret = (closes[idx + n] / base - 1.0) * 100.0
                row_item[key] = ret
                metrics.setdefault(key, {"values": []})["values"].append(ret)
            else:
                row_item[key] = None
        realized_rows.append(row_item)

    horizon_stats: Dict[str, Any] = {}
    for n in horizons:
        key = f"d{n}"
        vals = metrics.get(key, {}).get("values", [])
        if not vals:
            horizon_stats[key] = {"sample_count": 0, "win_rate": None, "avg_return": None}
            continue
        arr = np.array(vals, dtype=float)
        win_rate = float((arr > 0).sum() / len(arr) * 100.0)
        avg_return = float(arr.mean())
        horizon_stats[key] = {
            "sample_count": int(len(arr)),
            "win_rate": round(win_rate, 2),
            "avg_return": round(avg_return, 2),
        }

    realized_rows = sorted(realized_rows, key=lambda x: str(x.get("signal_date") or ""), reverse=True)[:10]
    return _sanitize(
        {
            "ok": True,
            "code": code6,
            "strategy_version": sv,
            "sample_count": int(valid_sample_count),
            "horizons": horizon_stats,
            "recent_samples": realized_rows,
        }
    )


