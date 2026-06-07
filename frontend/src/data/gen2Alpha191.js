// Generated from the Guotai Junan Alpha191 research report PDF on 2026-05-24.
// Source: https://guorn.com/static/upload/file/3/134065454575605.pdf
// Keep this file UTF-8. Formula text follows the PDF table extraction with field-name cleanup only.

export const alpha191Source = {
  name: 'GTJA Alpha191',
  title: '基于短周期价量特征的多因子选股体系：表 6 因子明细',
  url: 'https://guorn.com/static/upload/file/3/134065454575605.pdf',
  origin: '国泰君安证券研究，2017-06，数量化专题《基于短周期价量特征的多因子选股体系》',
  extraction: 'Local PDF text extraction from table 6; field-name cleanup applied for PDF line-wrap artifacts.'
}

export const alpha191Factors = [
  {
    "id": "Alpha001",
    "no": 1,
    "name": "GTJA 001",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "close",
      "volume"
    ],
    "formula": "(-1 * CORR(RANK(DELTA(LOG(VOLUME), 1)), RANK(((CLOSE - OPEN) / OPEN)), 6))"
  },
  {
    "id": "Alpha002",
    "no": 2,
    "name": "GTJA 002",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "(-1 * DELTA((((CLOSE - LOW) - (HIGH - CLOSE)) / (HIGH - LOW)), 1))"
  },
  {
    "id": "Alpha003",
    "no": 3,
    "name": "GTJA 003",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SUM((CLOSE=DELAY(CLOSE, 1)?0:CLOSE-(CLOSE>DELAY(CLOSE, 1)?MIN(LOW, DELAY(CLOSE, 1)):MAX(HIGH, DELAY(CLOSE, 1)))), 6)"
  },
  {
    "id": "Alpha004",
    "no": 4,
    "name": "GTJA 004",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "((((SUM(CLOSE, 8) / 8) + STD(CLOSE, 8)) < (SUM(CLOSE, 2) / 2)) ? (-1 * 1) : (((SUM(CLOSE, 2) / 2) < ((SUM(CLOSE, 8) / 8) - STD(CLOSE, 8))) ? 1 : (((1 < (VOLUME / MEAN(VOLUME, 20))) || ((VOLUME / MEAN(VOLUME, 20)) == 1)) ? 1 : (-1 * 1))))"
  },
  {
    "id": "Alpha005",
    "no": 5,
    "name": "GTJA 005",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "(-1 * TSMAX(CORR(TSRANK(VOLUME, 5), TSRANK(HIGH, 5), 5), 3))"
  },
  {
    "id": "Alpha006",
    "no": 6,
    "name": "GTJA 006",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "high"
    ],
    "formula": "(RANK(SIGN(DELTA((((OPEN * 0.85) + (HIGH * 0.15))), 4)))* -1)"
  },
  {
    "id": "Alpha007",
    "no": 7,
    "name": "GTJA 007",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((RANK(MAX((VWAP - CLOSE), 3)) + RANK(MIN((VWAP - CLOSE), 3))) * RANK(DELTA(VOLUME, 3)))"
  },
  {
    "id": "Alpha008",
    "no": 8,
    "name": "GTJA 008",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "vwap"
    ],
    "formula": "RANK(DELTA(((((HIGH + LOW) / 2) * 0.2) + (VWAP * 0.8)), 4) * -1)"
  },
  {
    "id": "Alpha009",
    "no": 9,
    "name": "GTJA 009",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "volume"
    ],
    "formula": "SMA(((HIGH+LOW)/2-(DELAY(HIGH, 1)+DELAY(LOW, 1))/2)*(HIGH-LOW)/VOLUME, 7, 2)"
  },
  {
    "id": "Alpha010",
    "no": 10,
    "name": "GTJA 010",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close",
      "returns"
    ],
    "formula": "(RANK(MAX(((RET < 0) ? STD(RET, 20) : CLOSE)^2), 5))"
  },
  {
    "id": "Alpha011",
    "no": 11,
    "name": "GTJA 011",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "SUM(((CLOSE-LOW)-(HIGH-CLOSE))./(HIGH-LOW).*VOLUME, 6)"
  },
  {
    "id": "Alpha012",
    "no": 12,
    "name": "GTJA 012",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "open",
      "close",
      "vwap"
    ],
    "formula": "(RANK((OPEN - (SUM(VWAP, 10) / 10)))) * (-1 * (RANK(ABS((CLOSE - VWAP)))))"
  },
  {
    "id": "Alpha013",
    "no": 13,
    "name": "GTJA 013",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "vwap"
    ],
    "formula": "(((HIGH * LOW)^0.5) - VWAP)"
  },
  {
    "id": "Alpha014",
    "no": 14,
    "name": "GTJA 014",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "CLOSE-DELAY(CLOSE, 5)"
  },
  {
    "id": "Alpha015",
    "no": 15,
    "name": "GTJA 015",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "close"
    ],
    "formula": "OPEN/DELAY(CLOSE, 1)-1"
  },
  {
    "id": "Alpha016",
    "no": 16,
    "name": "GTJA 016",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "volume",
      "vwap"
    ],
    "formula": "(-1 * TSMAX(RANK(CORR(RANK(VOLUME), RANK(VWAP), 5)), 5))"
  },
  {
    "id": "Alpha017",
    "no": 17,
    "name": "GTJA 017",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "close",
      "vwap"
    ],
    "formula": "RANK((VWAP - MAX(VWAP, 15)))^DELTA(CLOSE, 5)"
  },
  {
    "id": "Alpha018",
    "no": 18,
    "name": "GTJA 018",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "CLOSE/DELAY(CLOSE, 5)"
  },
  {
    "id": "Alpha019",
    "no": 19,
    "name": "GTJA 019",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE<DELAY(CLOSE, 5)?(CLOSE-DELAY(CLOSE, 5))/DELAY(CLOSE, 5):(CLOSE=DELAY(CLOSE, 5)?0:(CLOSE-DELAY(CLOSE, 5))/CLOSE))"
  },
  {
    "id": "Alpha020",
    "no": 20,
    "name": "GTJA 020",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE-DELAY(CLOSE, 6))/DELAY(CLOSE, 6)*100"
  },
  {
    "id": "Alpha021",
    "no": 21,
    "name": "GTJA 021",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "REGBETA(MEAN(CLOSE, 6), SEQUENCE(6))"
  },
  {
    "id": "Alpha022",
    "no": 22,
    "name": "GTJA 022",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMEAN(((CLOSE-MEAN(CLOSE, 6))/MEAN(CLOSE, 6)-DELAY((CLOSE-MEAN(CLOSE, 6))/MEAN(CLOSE, 6), 3)), 12, 1)"
  },
  {
    "id": "Alpha023",
    "no": 23,
    "name": "GTJA 023",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA((CLOSE>DELAY(CLOSE, 1)?STD(CLOSE:20), 0), 20, 1)/(SMA((CLOSE>DELAY(CLOSE, 1)?STD(CLOSE, 20):0), 20, 1)+SMA((CLOSE<=DELAY(CLOSE, 1)?STD(CLOSE, 20):0), 20, 1))*100"
  },
  {
    "id": "Alpha024",
    "no": 24,
    "name": "GTJA 024",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(CLOSE-DELAY(CLOSE, 5), 5, 1)"
  },
  {
    "id": "Alpha025",
    "no": 25,
    "name": "GTJA 025",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "returns"
    ],
    "formula": "((-1 * RANK((DELTA(CLOSE, 7) * (1 - RANK(DECAYLINEAR((VOLUME / MEAN(VOLUME, 20)), 9)))))) * (1 + RANK(SUM(RET, 250))))"
  },
  {
    "id": "Alpha026",
    "no": 26,
    "name": "GTJA 026",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "vwap"
    ],
    "formula": "((((SUM(CLOSE, 7) / 7) - CLOSE)) + ((CORR(VWAP, DELAY(CLOSE, 5), 230))))"
  },
  {
    "id": "Alpha027",
    "no": 27,
    "name": "GTJA 027",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "WMA((CLOSE-DELAY(CLOSE, 3))/DELAY(CLOSE, 3)*100+(CLOSE-DELAY(CLOSE, 6))/DELAY(CLOSE, 6)*100, 12)"
  },
  {
    "id": "Alpha028",
    "no": 28,
    "name": "GTJA 028",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "3*SMA((CLOSE-TSMIN(LOW, 9))/(TSMAX(HIGH, 9)-TSMIN(LOW, 9))*100, 3, 1)-2*SMA(SMA((CLOSE-TSMIN(LOW, 9))/(MAX(HIGH, 9)-TSMAX(LOW, 9))*100, 3, 1), 3, 1)"
  },
  {
    "id": "Alpha029",
    "no": 29,
    "name": "GTJA 029",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(CLOSE-DELAY(CLOSE, 6))/DELAY(CLOSE, 6)*VOLUME"
  },
  {
    "id": "Alpha030",
    "no": 30,
    "name": "GTJA 030",
    "theme": "benchmark_style",
    "status": "data_gap",
    "fields": [
      "close",
      "mkt",
      "smb",
      "hml"
    ],
    "formula": "WMA((REGRESI(CLOSE/DELAY(CLOSE)-1, MKT, SMB, HML, 60))^2, 20)"
  },
  {
    "id": "Alpha031",
    "no": 31,
    "name": "GTJA 031",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE-MEAN(CLOSE, 12))/MEAN(CLOSE, 12)*100"
  },
  {
    "id": "Alpha032",
    "no": 32,
    "name": "GTJA 032",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "(-1 * SUM(RANK(CORR(RANK(HIGH), RANK(VOLUME), 3)), 3))"
  },
  {
    "id": "Alpha033",
    "no": 33,
    "name": "GTJA 033",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "returns"
    ],
    "formula": "((((-1 * TSMIN(LOW, 5)) + DELAY(TSMIN(LOW, 5), 5)) * RANK(((SUM(RET, 240) - SUM(RET, 20)) / 220))) * TSRANK(VOLUME, 5))"
  },
  {
    "id": "Alpha034",
    "no": 34,
    "name": "GTJA 034",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MEAN(CLOSE, 12)/CLOSE"
  },
  {
    "id": "Alpha035",
    "no": 35,
    "name": "GTJA 035",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume"
    ],
    "formula": "(MIN(RANK(DECAYLINEAR(DELTA(OPEN, 1), 15)), RANK(DECAYLINEAR(CORR((VOLUME), ((OPEN * 0.65) + (OPEN *0.35)), 17), 7))) * -1)"
  },
  {
    "id": "Alpha036",
    "no": 36,
    "name": "GTJA 036",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "volume",
      "vwap"
    ],
    "formula": "RANK(SUM(CORR(RANK(VOLUME), RANK(VWAP)), 6), 2)"
  },
  {
    "id": "Alpha037",
    "no": 37,
    "name": "GTJA 037",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "returns"
    ],
    "formula": "(-1 * RANK(((SUM(OPEN, 5) * SUM(RET, 5)) - DELAY((SUM(OPEN, 5) * SUM(RET, 5)), 10))))"
  },
  {
    "id": "Alpha038",
    "no": 38,
    "name": "GTJA 038",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high"
    ],
    "formula": "(((SUM(HIGH, 20) / 20) < HIGH) ? (-1 * DELTA(HIGH, 2)) : 0)"
  },
  {
    "id": "Alpha039",
    "no": 39,
    "name": "GTJA 039",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((RANK(DECAYLINEAR(DELTA((CLOSE), 2), 8)) - RANK(DECAYLINEAR(CORR(((VWAP * 0.3) + (OPEN * 0.7)), SUM(MEAN(VOLUME, 180), 37), 14), 12))) * -1)"
  },
  {
    "id": "Alpha040",
    "no": 40,
    "name": "GTJA 040",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "SUM((CLOSE>DELAY(CLOSE, 1)?VOLUME:0), 26)/SUM((CLOSE<=DELAY(CLOSE, 1)?VOLUME:0), 26)*100"
  },
  {
    "id": "Alpha041",
    "no": 41,
    "name": "GTJA 041",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "vwap"
    ],
    "formula": "(RANK(MAX(DELTA((VWAP), 3), 5))* -1)"
  },
  {
    "id": "Alpha042",
    "no": 42,
    "name": "GTJA 042",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "((-1 * RANK(STD(HIGH, 10))) * CORR(HIGH, VOLUME, 10))"
  },
  {
    "id": "Alpha043",
    "no": 43,
    "name": "GTJA 043",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "SUM((CLOSE>DELAY(CLOSE, 1)?VOLUME:(CLOSE<DELAY(CLOSE, 1)?-VOLUME:0)), 6)"
  },
  {
    "id": "Alpha044",
    "no": 44,
    "name": "GTJA 044",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "vwap"
    ],
    "formula": "(TSRANK(DECAYLINEAR(CORR(((LOW)), MEAN(VOLUME, 10), 7), 6), 4) + TSRANK(DECAYLINEAR(DELTA((VWAP), 3), 10), 15))"
  },
  {
    "id": "Alpha045",
    "no": 45,
    "name": "GTJA 045",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "close",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(DELTA((((CLOSE * 0.6) + (OPEN *0.4))), 1)) * RANK(CORR(VWAP, MEAN(VOLUME, 150), 15)))"
  },
  {
    "id": "Alpha046",
    "no": 46,
    "name": "GTJA 046",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(MEAN(CLOSE, 3)+MEAN(CLOSE, 6)+MEAN(CLOSE, 12)+MEAN(CLOSE, 24))/(4*CLOSE)"
  },
  {
    "id": "Alpha047",
    "no": 47,
    "name": "GTJA 047",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA((TSMAX(HIGH, 6)-CLOSE)/(TSMAX(HIGH, 6)-TSMIN(LOW, 6))*100, 9, 1)"
  },
  {
    "id": "Alpha048",
    "no": 48,
    "name": "GTJA 048",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(-1*((RANK(((SIGN((CLOSE - DELAY(CLOSE, 1))) + SIGN((DELAY(CLOSE, 1) - DELAY(CLOSE, 2)))) + SIGN((DELAY(CLOSE, 2) - DELAY(CLOSE, 3)))))) * SUM(VOLUME, 5)) / SUM(VOLUME, 20))"
  },
  {
    "id": "Alpha049",
    "no": 49,
    "name": "GTJA 049",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)/(SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)+SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12))"
  },
  {
    "id": "Alpha050",
    "no": 50,
    "name": "GTJA 050",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)/(SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)+SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12))-SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)/(SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0: MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)+SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELA Y(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12))"
  },
  {
    "id": "Alpha051",
    "no": 51,
    "name": "GTJA 051",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)/(SUM(((HIGH+LOW)<=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12)+SUM(((HIGH+LOW)>=(DELAY(HIGH, 1)+DELAY(LOW, 1))?0:MAX(ABS(HIGH-DELAY(HIGH, 1)), ABS(LOW-DELAY(LOW, 1)))), 12))"
  },
  {
    "id": "Alpha052",
    "no": 52,
    "name": "GTJA 052",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SUM(MAX(0, HIGH-DELAY((HIGH+LOW+CLOSE)/3, 1)), 26)/SUM(MAX(0, DELAY((HIGH+LOW+CLOSE)/3, 1)-L), 26)* 100"
  },
  {
    "id": "Alpha053",
    "no": 53,
    "name": "GTJA 053",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "COUNT(CLOSE>DELAY(CLOSE, 1), 12)/12*100"
  },
  {
    "id": "Alpha054",
    "no": 54,
    "name": "GTJA 054",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "close"
    ],
    "formula": "(-1 * RANK((STD(ABS(CLOSE - OPEN)) + (CLOSE - OPEN)) + CORR(CLOSE, OPEN, 10)))"
  },
  {
    "id": "Alpha055",
    "no": 55,
    "name": "GTJA 055",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "close"
    ],
    "formula": "SUM(16*(CLOSE-DELAY(CLOSE, 1)+(CLOSE-OPEN)/2+DELAY(CLOSE, 1)-DELAY(OPEN, 1))/((ABS(HIGH-DELAY(CLOSE, 1))>ABS(LOW-DELAY(CLOSE, 1)) & ABS(HIGH-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(LOW, 1))?ABS(HIGH-DELAY(CLOSE, 1))+ABS(LOW-DELAY(CLOSE, 1))/2+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4:(ABS(LOW-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(LOW, 1)) & ABS(LOW-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(CLOSE, 1))?ABS(LOW-DELAY(CLOSE, 1))+ABS(HIGH-DELAY(CLOSE, 1))/2+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4:ABS(HIGH-DELAY(LOW, 1))+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4)))*MAX(ABS(HIGH-DELAY(CLOSE, 1)), ABS(LOW-DELAY(CLOSE, 1))), 20)"
  },
  {
    "id": "Alpha056",
    "no": 56,
    "name": "GTJA 056",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "volume"
    ],
    "formula": "(RANK((OPEN - TSMIN(OPEN, 12))) < RANK((RANK(CORR(SUM(((HIGH + LOW) / 2), 19), SUM(MEAN(VOLUME, 40), 19), 13))^5)))"
  },
  {
    "id": "Alpha057",
    "no": 57,
    "name": "GTJA 057",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA((CLOSE-TSMIN(LOW, 9))/(TSMAX(HIGH, 9)-TSMIN(LOW, 9))*100, 3, 1)"
  },
  {
    "id": "Alpha058",
    "no": 58,
    "name": "GTJA 058",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "COUNT(CLOSE>DELAY(CLOSE, 1), 20)/20*100"
  },
  {
    "id": "Alpha059",
    "no": 59,
    "name": "GTJA 059",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SUM((CLOSE=DELAY(CLOSE, 1)?0:CLOSE-(CLOSE>DELAY(CLOSE, 1)?MIN(LOW, DELAY(CLOSE, 1)):MAX(HIGH, DELAY(CLOSE, 1)))), 20)"
  },
  {
    "id": "Alpha060",
    "no": 60,
    "name": "GTJA 060",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "SUM(((CLOSE-LOW)-(HIGH-CLOSE))./(HIGH-LOW).*VOLUME, 20)"
  },
  {
    "id": "Alpha061",
    "no": 61,
    "name": "GTJA 061",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "vwap"
    ],
    "formula": "(MAX(RANK(DECAYLINEAR(DELTA(VWAP, 1), 12)), RANK(DECAYLINEAR(RANK(CORR((LOW), MEAN(VOLUME, 80), 8)), 17))) * -1)"
  },
  {
    "id": "Alpha062",
    "no": 62,
    "name": "GTJA 062",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "(-1 * CORR(HIGH, RANK(VOLUME), 5))"
  },
  {
    "id": "Alpha063",
    "no": 63,
    "name": "GTJA 063",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 6, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 6, 1)*100"
  },
  {
    "id": "Alpha064",
    "no": 64,
    "name": "GTJA 064",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "(MAX(RANK(DECAYLINEAR(CORR(RANK(VWAP), RANK(VOLUME), 4), 4)), RANK(DECAYLINEAR(MAX(CORR(RANK(CLOSE), RANK(MEAN(VOLUME, 60)), 4), 13), 14))) * -1)"
  },
  {
    "id": "Alpha065",
    "no": 65,
    "name": "GTJA 065",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MEAN(CLOSE, 6)/CLOSE"
  },
  {
    "id": "Alpha066",
    "no": 66,
    "name": "GTJA 066",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE-MEAN(CLOSE, 6))/MEAN(CLOSE, 6)*100"
  },
  {
    "id": "Alpha067",
    "no": 67,
    "name": "GTJA 067",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 24, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 24, 1)*100"
  },
  {
    "id": "Alpha068",
    "no": 68,
    "name": "GTJA 068",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "volume"
    ],
    "formula": "SMA(((HIGH+LOW)/2-(DELAY(HIGH, 1)+DELAY(LOW, 1))/2)*(HIGH-LOW)/VOLUME, 15, 2)"
  },
  {
    "id": "Alpha069",
    "no": 69,
    "name": "GTJA 069",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "dtm",
      "dbm"
    ],
    "formula": "(SUM(DTM, 20)>SUM(DBM, 20) ？(SUM(DTM, 20)-SUM(DBM, 20))/SUM(DTM, 20)：(SUM(DTM, 20)=SUM(DBM, 20)？ 0：(SUM(DTM, 20)-SUM(DBM, 20))/SUM(DBM, 20)))"
  },
  {
    "id": "Alpha070",
    "no": 70,
    "name": "GTJA 070",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "amount"
    ],
    "formula": "STD(AMOUNT, 6)"
  },
  {
    "id": "Alpha071",
    "no": 71,
    "name": "GTJA 071",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE-MEAN(CLOSE, 24))/MEAN(CLOSE, 24)*100"
  },
  {
    "id": "Alpha072",
    "no": 72,
    "name": "GTJA 072",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA((TSMAX(HIGH, 6)-CLOSE)/(TSMAX(HIGH, 6)-TSMIN(LOW, 6))*100, 15, 1)"
  },
  {
    "id": "Alpha073",
    "no": 73,
    "name": "GTJA 073",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((TSRANK(DECAYLINEAR(DECAYLINEAR(CORR((CLOSE), VOLUME, 10), 16), 4), 5) - RANK(DECAYLINEAR(CORR(VWAP, MEAN(VOLUME, 30), 4), 3))) * -1)"
  },
  {
    "id": "Alpha074",
    "no": 74,
    "name": "GTJA 074",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(CORR(SUM(((LOW * 0.35) + (VWAP * 0.65)), 20), SUM(MEAN(VOLUME, 40), 20), 7)) + RANK(CORR(RANK(VWAP), RANK(VOLUME), 6)))"
  },
  {
    "id": "Alpha075",
    "no": 75,
    "name": "GTJA 075",
    "theme": "benchmark_style",
    "status": "data_gap",
    "fields": [
      "benchmark_close",
      "benchmark_open",
      "open",
      "close"
    ],
    "formula": "COUNT(CLOSE>OPEN & BENCHMARKINDEXCLOSE<BENCHMARKINDEXOPEN, 50)/COUNT(BENCHMARKINDEXCLOSE<BENCHMARKINDEXOPEN, 50)"
  },
  {
    "id": "Alpha076",
    "no": 76,
    "name": "GTJA 076",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "STD(ABS((CLOSE/DELAY(CLOSE, 1)-1))/VOLUME, 20)/MEAN(ABS((CLOSE/DELAY(CLOSE, 1)-1))/VOLUME, 20)"
  },
  {
    "id": "Alpha077",
    "no": 77,
    "name": "GTJA 077",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "volume",
      "vwap"
    ],
    "formula": "MIN(RANK(DECAYLINEAR(((((HIGH + LOW) / 2) + HIGH) - (VWAP + HIGH)), 20)), RANK(DECAYLINEAR(CORR(((HIGH + LOW) / 2), MEAN(VOLUME, 40), 3), 6)))"
  },
  {
    "id": "Alpha078",
    "no": 78,
    "name": "GTJA 078",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "((HIGH+LOW+CLOSE)/3-MA((HIGH+LOW+CLOSE)/3, 12))/(0.015*MEAN(ABS(CLOSE-MEAN((HIGH+LOW+CLOSE)/3, 12)), 12))"
  },
  {
    "id": "Alpha079",
    "no": 79,
    "name": "GTJA 079",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 12, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 12, 1)*100"
  },
  {
    "id": "Alpha080",
    "no": 80,
    "name": "GTJA 080",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "(VOLUME-DELAY(VOLUME, 5))/DELAY(VOLUME, 5)*100"
  },
  {
    "id": "Alpha081",
    "no": 81,
    "name": "GTJA 081",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "SMA(VOLUME, 21, 2)"
  },
  {
    "id": "Alpha082",
    "no": 82,
    "name": "GTJA 082",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA((TSMAX(HIGH, 6)-CLOSE)/(TSMAX(HIGH, 6)-TSMIN(LOW, 6))*100, 20, 1)"
  },
  {
    "id": "Alpha083",
    "no": 83,
    "name": "GTJA 083",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "(-1 * RANK(COVIANCE(RANK(HIGH), RANK(VOLUME), 5)))"
  },
  {
    "id": "Alpha084",
    "no": 84,
    "name": "GTJA 084",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "SUM((CLOSE>DELAY(CLOSE, 1)?VOLUME:(CLOSE<DELAY(CLOSE, 1)?-VOLUME:0)), 20)"
  },
  {
    "id": "Alpha085",
    "no": 85,
    "name": "GTJA 085",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(TSRANK((VOLUME / MEAN(VOLUME, 20)), 20) * TSRANK((-1 * DELTA(CLOSE, 7)), 8))"
  },
  {
    "id": "Alpha086",
    "no": 86,
    "name": "GTJA 086",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "((0.25 < (((DELAY(CLOSE, 20) - DELAY(CLOSE, 10)) / 10) - ((DELAY(CLOSE, 10) - CLOSE) / 10))) ? (-1 * 1) : (((((DELAY(CLOSE, 20) - DELAY(CLOSE, 10)) / 10) - ((DELAY(CLOSE, 10) - CLOSE) / 10)) < 0) ? 1 : ((-1 * 1) * (CLOSE - DELAY(CLOSE, 1)))))"
  },
  {
    "id": "Alpha087",
    "no": 87,
    "name": "GTJA 087",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "vwap"
    ],
    "formula": "((RANK(DECAYLINEAR(DELTA(VWAP, 4), 7)) + TSRANK(DECAYLINEAR(((((LOW * 0.9) + (LOW * 0.1)) - VWAP) / (OPEN - ((HIGH + LOW) / 2))), 11), 7)) * -1)"
  },
  {
    "id": "Alpha088",
    "no": 88,
    "name": "GTJA 088",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(CLOSE-DELAY(CLOSE, 20))/DELAY(CLOSE, 20)*100"
  },
  {
    "id": "Alpha089",
    "no": 89,
    "name": "GTJA 089",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "2*(SMA(CLOSE, 13, 2)-SMA(CLOSE, 27, 2)-SMA(SMA(CLOSE, 13, 2)-SMA(CLOSE, 27, 2), 10, 2))"
  },
  {
    "id": "Alpha090",
    "no": 90,
    "name": "GTJA 090",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "volume",
      "vwap"
    ],
    "formula": "(RANK(CORR(RANK(VWAP), RANK(VOLUME), 5)) * -1)"
  },
  {
    "id": "Alpha091",
    "no": 91,
    "name": "GTJA 091",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "close",
      "volume"
    ],
    "formula": "((RANK((CLOSE - MAX(CLOSE, 5)))*RANK(CORR((MEAN(VOLUME, 40)), LOW, 5))) * -1)"
  },
  {
    "id": "Alpha092",
    "no": 92,
    "name": "GTJA 092",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "(MAX(RANK(DECAYLINEAR(DELTA(((CLOSE * 0.35) + (VWAP *0.65)), 2), 3)), TSRANK(DECAYLINEAR(ABS(CORR((MEAN(VOLUME, 180)), CLOSE, 13)), 5), 15)) * -1)"
  },
  {
    "id": "Alpha093",
    "no": 93,
    "name": "GTJA 093",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "low"
    ],
    "formula": "SUM((OPEN>=DELAY(OPEN, 1)?0:MAX((OPEN-LOW), (OPEN-DELAY(OPEN, 1)))), 20)"
  },
  {
    "id": "Alpha094",
    "no": 94,
    "name": "GTJA 094",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "SUM((CLOSE>DELAY(CLOSE, 1)?VOLUME:(CLOSE<DELAY(CLOSE, 1)?-VOLUME:0)), 30)"
  },
  {
    "id": "Alpha095",
    "no": 95,
    "name": "GTJA 095",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "amount"
    ],
    "formula": "STD(AMOUNT, 20)"
  },
  {
    "id": "Alpha096",
    "no": 96,
    "name": "GTJA 096",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA(SMA((CLOSE-TSMIN(LOW, 9))/(TSMAX(HIGH, 9)-TSMIN(LOW, 9))*100, 3, 1), 3, 1)"
  },
  {
    "id": "Alpha097",
    "no": 97,
    "name": "GTJA 097",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "STD(VOLUME, 10)"
  },
  {
    "id": "Alpha098",
    "no": 98,
    "name": "GTJA 098",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "((((DELTA((SUM(CLOSE, 100) / 100), 100) / DELAY(CLOSE, 100)) < 0.05) || ((DELTA((SUM(CLOSE, 100) / 100), 100) / DELAY(CLOSE, 100)) == 0.05)) ? (-1 * (CLOSE - TSMIN(CLOSE, 100))) : (-1 * DELTA(CLOSE, 3)))"
  },
  {
    "id": "Alpha099",
    "no": 99,
    "name": "GTJA 099",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(-1 * RANK(COVIANCE(RANK(CLOSE), RANK(VOLUME), 5)))"
  },
  {
    "id": "Alpha100",
    "no": 100,
    "name": "GTJA 100",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "STD(VOLUME, 20)"
  },
  {
    "id": "Alpha101",
    "no": 101,
    "name": "GTJA 101",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((RANK(CORR(CLOSE, SUM(MEAN(VOLUME, 30), 37), 15)) < RANK(CORR(RANK(((HIGH * 0.1) + (VWAP * 0.9))), RANK(VOLUME), 11))) * -1)"
  },
  {
    "id": "Alpha102",
    "no": 102,
    "name": "GTJA 102",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "SMA(MAX(VOLUME-DELAY(VOLUME, 1), 0), 6, 1)/SMA(ABS(VOLUME-DELAY(VOLUME, 1)), 6, 1)*100"
  },
  {
    "id": "Alpha103",
    "no": 103,
    "name": "GTJA 103",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "low"
    ],
    "formula": "((20-LOWDAY(LOW, 20))/20)*100"
  },
  {
    "id": "Alpha104",
    "no": 104,
    "name": "GTJA 104",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "close",
      "volume"
    ],
    "formula": "(-1 * (DELTA(CORR(HIGH, VOLUME, 5), 5) * RANK(STD(CLOSE, 20))))"
  },
  {
    "id": "Alpha105",
    "no": 105,
    "name": "GTJA 105",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume"
    ],
    "formula": "(-1 * CORR(RANK(OPEN), RANK(VOLUME), 10))"
  },
  {
    "id": "Alpha106",
    "no": 106,
    "name": "GTJA 106",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "CLOSE-DELAY(CLOSE, 20)"
  },
  {
    "id": "Alpha107",
    "no": 107,
    "name": "GTJA 107",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "close"
    ],
    "formula": "(((-1 * RANK((OPEN - DELAY(HIGH, 1)))) * RANK((OPEN - DELAY(CLOSE, 1)))) * RANK((OPEN - DELAY(LOW, 1))))"
  },
  {
    "id": "Alpha108",
    "no": 108,
    "name": "GTJA 108",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume",
      "vwap"
    ],
    "formula": "((RANK((HIGH - MIN(HIGH, 2)))^RANK(CORR((VWAP), (MEAN(VOLUME, 120)), 6))) * -1)"
  },
  {
    "id": "Alpha109",
    "no": 109,
    "name": "GTJA 109",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "SMA(HIGH-LOW, 10, 2)/SMA(SMA(HIGH-LOW, 10, 2), 10, 2)"
  },
  {
    "id": "Alpha110",
    "no": 110,
    "name": "GTJA 110",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SUM(MAX(0, HIGH-DELAY(CLOSE, 1)), 20)/SUM(MAX(0, DELAY(CLOSE, 1)-LOW), 20)*100"
  },
  {
    "id": "Alpha111",
    "no": 111,
    "name": "GTJA 111",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "SMA(VOL*((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW), 11, 2)-SMA(VOL*((CLOSE-LOW)-(HIGH-CLOSE))/(HIGH-LOW), 4, 2)"
  },
  {
    "id": "Alpha112",
    "no": 112,
    "name": "GTJA 112",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(SUM((CLOSE-DELAY(CLOSE, 1)>0?CLOSE-DELAY(CLOSE, 1):0), 12)-SUM((CLOSE-DELAY(CLOSE, 1)<0?ABS(CLOSE-DELAY(CLOSE, 1)):0), 12))/(SUM((CLOSE-DELAY(CLOSE, 1)>0?CLOSE-DELAY(CLOSE, 1):0), 12)+SUM((CLOSE-DELAY(CLOSE, 1)<0?ABS(CLOSE-DELAY(CLOSE, 1)):0), 12))*100"
  },
  {
    "id": "Alpha113",
    "no": 113,
    "name": "GTJA 113",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(-1 * ((RANK((SUM(DELAY(CLOSE, 5), 20) / 20)) * CORR(CLOSE, VOLUME, 2)) * RANK(CORR(SUM(CLOSE, 5), SUM(CLOSE, 20), 2))))"
  },
  {
    "id": "Alpha114",
    "no": 114,
    "name": "GTJA 114",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((RANK(DELAY(((HIGH - LOW) / (SUM(CLOSE, 5) / 5)), 2)) * RANK(RANK(VOLUME))) / (((HIGH - LOW) / (SUM(CLOSE, 5) / 5)) / (VWAP - CLOSE)))"
  },
  {
    "id": "Alpha115",
    "no": 115,
    "name": "GTJA 115",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "(RANK(CORR(((HIGH * 0.9) + (CLOSE * 0.1)), MEAN(VOLUME, 30), 10))^RANK(CORR(TSRANK(((HIGH + LOW) / 2), 4), TSRANK(VOLUME, 10), 7)))"
  },
  {
    "id": "Alpha116",
    "no": 116,
    "name": "GTJA 116",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "REGBETA(CLOSE, SEQUENCE, 20)"
  },
  {
    "id": "Alpha117",
    "no": 117,
    "name": "GTJA 117",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume",
      "returns"
    ],
    "formula": "((TSRANK(VOLUME, 32) * (1 - TSRANK(((CLOSE + HIGH) - LOW), 16))) * (1 - TSRANK(RET, 32)))"
  },
  {
    "id": "Alpha118",
    "no": 118,
    "name": "GTJA 118",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low"
    ],
    "formula": "SUM(HIGH-OPEN, 20)/SUM(OPEN-LOW, 20)*100"
  },
  {
    "id": "Alpha119",
    "no": 119,
    "name": "GTJA 119",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(DECAYLINEAR(CORR(VWAP, SUM(MEAN(VOLUME, 5), 26), 5), 7)) - RANK(DECAYLINEAR(TSRANK(MIN(CORR(RANK(OPEN), RANK(MEAN(VOLUME, 15)), 21), 9), 7), 8)))"
  },
  {
    "id": "Alpha120",
    "no": 120,
    "name": "GTJA 120",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "close",
      "vwap"
    ],
    "formula": "(RANK((VWAP - CLOSE)) / RANK((VWAP + CLOSE)))"
  },
  {
    "id": "Alpha121",
    "no": 121,
    "name": "GTJA 121",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "volume",
      "vwap"
    ],
    "formula": "((RANK((VWAP - MIN(VWAP, 12)))^TSRANK(CORR(TSRANK(VWAP, 20), TSRANK(MEAN(VOLUME, 60), 2), 18), 3)) * -1)"
  },
  {
    "id": "Alpha122",
    "no": 122,
    "name": "GTJA 122",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(SMA(SMA(SMA(LOG(CLOSE), 13, 2), 13, 2), 13, 2)-DELAY(SMA(SMA(SMA(LOG(CLOSE), 13, 2), 13, 2), 13, 2), 1))/DELAY(SM A(SMA(SMA(LOG(CLOSE), 13, 2), 13, 2), 13, 2), 1)"
  },
  {
    "id": "Alpha123",
    "no": 123,
    "name": "GTJA 123",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "volume"
    ],
    "formula": "((RANK(CORR(SUM(((HIGH + LOW) / 2), 20), SUM(MEAN(VOLUME, 60), 20), 9)) < RANK(CORR(LOW, VOLUME, 6))) * -1)"
  },
  {
    "id": "Alpha124",
    "no": 124,
    "name": "GTJA 124",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "close",
      "vwap"
    ],
    "formula": "(CLOSE - VWAP) / DECAYLINEAR(RANK(TSMAX(CLOSE, 30)), 2)"
  },
  {
    "id": "Alpha125",
    "no": 125,
    "name": "GTJA 125",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(DECAYLINEAR(CORR((VWAP), MEAN(VOLUME, 80), 17), 20)) / RANK(DECAYLINEAR(DELTA(((CLOSE * 0.5) + (VWAP * 0.5)), 3), 16)))"
  },
  {
    "id": "Alpha126",
    "no": 126,
    "name": "GTJA 126",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "(CLOSE+HIGH+LOW)/3"
  },
  {
    "id": "Alpha127",
    "no": 127,
    "name": "GTJA 127",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(MEAN((100*(CLOSE-MAX(CLOSE, 12))/(MAX(CLOSE, 12)))^2))^(1/2)"
  },
  {
    "id": "Alpha128",
    "no": 128,
    "name": "GTJA 128",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "100-(100/(1+SUM(((HIGH+LOW+CLOSE)/3>DELAY((HIGH+LOW+CLOSE)/3, 1)?(HIGH+LOW+CLOSE)/3*VOLUME:0), 14)/SUM(((HIGH+LOW+CLOSE)/3<DELAY((HIGH+LOW+CLOSE)/3, 1)?(HIGH+LOW+CLOSE)/3*VOLUME:0), 14)))"
  },
  {
    "id": "Alpha129",
    "no": 129,
    "name": "GTJA 129",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SUM((CLOSE-DELAY(CLOSE, 1)<0?ABS(CLOSE-DELAY(CLOSE, 1)):0), 12)"
  },
  {
    "id": "Alpha130",
    "no": 130,
    "name": "GTJA 130",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(DECAYLINEAR(CORR(((HIGH + LOW) / 2), MEAN(VOLUME, 40), 9), 10)) / RANK(DECAYLINEAR(CORR(RANK(VWAP), RANK(VOLUME), 7), 3)))"
  },
  {
    "id": "Alpha131",
    "no": 131,
    "name": "GTJA 131",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "close",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(DELAT(VWAP, 1))^TSRANK(CORR(CLOSE, MEAN(VOLUME, 50), 18), 18))"
  },
  {
    "id": "Alpha132",
    "no": 132,
    "name": "GTJA 132",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "amount"
    ],
    "formula": "MEAN(AMOUNT, 20)"
  },
  {
    "id": "Alpha133",
    "no": 133,
    "name": "GTJA 133",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "((20-HIGHDAY(HIGH, 20))/20)*100-((20-LOWDAY(LOW, 20))/20)*100"
  },
  {
    "id": "Alpha134",
    "no": 134,
    "name": "GTJA 134",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(CLOSE-DELAY(CLOSE, 12))/DELAY(CLOSE, 12)*VOLUME"
  },
  {
    "id": "Alpha135",
    "no": 135,
    "name": "GTJA 135",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(DELAY(CLOSE/DELAY(CLOSE, 20), 1), 20, 1)"
  },
  {
    "id": "Alpha136",
    "no": 136,
    "name": "GTJA 136",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume",
      "returns"
    ],
    "formula": "((-1 * RANK(DELTA(RET, 3))) * CORR(OPEN, VOLUME, 10))"
  },
  {
    "id": "Alpha137",
    "no": 137,
    "name": "GTJA 137",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "close"
    ],
    "formula": "16*(CLOSE-DELAY(CLOSE, 1)+(CLOSE-OPEN)/2+DELAY(CLOSE, 1)-DELAY(OPEN, 1))/((ABS(HIGH-DELAY(CLOSE, 1))>ABS(LOW-DELAY(CLOSE, 1)) & ABS(HIGH-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(LOW, 1))?ABS(HIGH-DELAY(CLOSE, 1))+ABS(LOW-DELAY(CLOSE, 1))/2+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4:(ABS(LOW-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(LOW, 1)) & ABS(LOW-DELAY(CLOSE, 1))>ABS(HIGH-DELAY(CLOSE, 1))?ABS(LOW-DELAY(CLOSE, 1))+ABS(HIGH-DELAY(CLOSE, 1))/2+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4:ABS(HIGH-DELAY(LOW, 1))+ABS(DELAY(CLOSE, 1)-DELAY(OPEN, 1))/4)))*MAX(ABS(HIGH-DELAY(CLOSE, 1)), ABS(LOW-DELAY(CLOSE, 1)))"
  },
  {
    "id": "Alpha138",
    "no": 138,
    "name": "GTJA 138",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "vwap"
    ],
    "formula": "((RANK(DECAYLINEAR(DELTA((((LOW * 0.7) + (VWAP *0.3))), 3), 20)) - TSRANK(DECAYLINEAR(TSRANK(CORR(TSRANK(LOW, 8), TSRANK(MEAN(VOLUME, 60), 17), 5), 19), 16), 7)) * -1)"
  },
  {
    "id": "Alpha139",
    "no": 139,
    "name": "GTJA 139",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume"
    ],
    "formula": "(-1 * CORR(OPEN, VOLUME, 10))"
  },
  {
    "id": "Alpha140",
    "no": 140,
    "name": "GTJA 140",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "MIN(RANK(DECAYLINEAR(((RANK(OPEN) + RANK(LOW)) - (RANK(HIGH) + RANK(CLOSE))), 8)), TSRANK(DECAYLINEAR(CORR(TSRANK(CLOSE, 8), TSRANK(MEAN(VOLUME, 60), 20), 8), 7), 3))"
  },
  {
    "id": "Alpha141",
    "no": 141,
    "name": "GTJA 141",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "volume"
    ],
    "formula": "(RANK(CORR(RANK(HIGH), RANK(MEAN(VOLUME, 15)), 9))* -1)"
  },
  {
    "id": "Alpha142",
    "no": 142,
    "name": "GTJA 142",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(((-1 * RANK(TSRANK(CLOSE, 10))) * RANK(DELTA(DELTA(CLOSE, 1), 1))) * RANK(TSRANK((VOLUME /MEAN(VOLUME, 20)), 5)))"
  },
  {
    "id": "Alpha143",
    "no": 143,
    "name": "GTJA 143",
    "theme": "momentum_reversal",
    "status": "data_gap",
    "fields": [
      "close",
      "self"
    ],
    "formula": "CLOSE>DELAY(CLOSE, 1)?(CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)*SELF:SELF"
  },
  {
    "id": "Alpha144",
    "no": 144,
    "name": "GTJA 144",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "amount"
    ],
    "formula": "SUMIF(ABS(CLOSE/DELAY(CLOSE, 1)-1)/AMOUNT, 20, CLOSE<DELAY(CLOSE, 1))/COUNT(CLOSE<DELAY(CLOSE, 1), 20)"
  },
  {
    "id": "Alpha145",
    "no": 145,
    "name": "GTJA 145",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "(MEAN(VOLUME, 9)-MEAN(VOLUME, 26))/MEAN(VOLUME, 12)*100"
  },
  {
    "id": "Alpha146",
    "no": 146,
    "name": "GTJA 146",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MEAN((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)-SMA((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1), 61, 2), 20)*((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)-SMA((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1), 61, 2))/SMA(((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)-((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)-SMA((CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1), 61, 2)))^2, 60);"
  },
  {
    "id": "Alpha147",
    "no": 147,
    "name": "GTJA 147",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "REGBETA(MEAN(CLOSE, 12), SEQUENCE(12))"
  },
  {
    "id": "Alpha148",
    "no": 148,
    "name": "GTJA 148",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "volume"
    ],
    "formula": "((RANK(CORR((OPEN), SUM(MEAN(VOLUME, 60), 9), 6)) < RANK((OPEN - TSMIN(OPEN, 14)))) * -1)"
  },
  {
    "id": "Alpha149",
    "no": 149,
    "name": "GTJA 149",
    "theme": "benchmark_style",
    "status": "data_gap",
    "fields": [
      "benchmark_close",
      "close"
    ],
    "formula": "REGBETA(FILTER(CLOSE/DELAY(CLOSE, 1)-1, BENCHMARKINDEXCLOSE<DELAY(BENCHMARKINDEXCLOSE, 1)), FILTER(BENCHMARKINDEXCLOSE/DELAY(BENCHMARKINDEXCLOSE, 1)-1, BENCHMARKINDEXCLOSE<DELA Y(BENCHMARKINDEXCLOSE, 1)), 252)"
  },
  {
    "id": "Alpha150",
    "no": 150,
    "name": "GTJA 150",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "(CLOSE+HIGH+LOW)/3*VOLUME"
  },
  {
    "id": "Alpha151",
    "no": 151,
    "name": "GTJA 151",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(CLOSE-DELAY(CLOSE, 20), 20, 1)"
  },
  {
    "id": "Alpha152",
    "no": 152,
    "name": "GTJA 152",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(MEAN(DELAY(SMA(DELAY(CLOSE/DELAY(CLOSE, 9), 1), 9, 1), 1), 12)-MEAN(DELAY(SMA(DELAY(CLOSE/DELAY (CLOSE, 9), 1), 9, 1), 1), 26), 9, 1)"
  },
  {
    "id": "Alpha153",
    "no": 153,
    "name": "GTJA 153",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(MEAN(CLOSE, 3)+MEAN(CLOSE, 6)+MEAN(CLOSE, 12)+MEAN(CLOSE, 24))/4"
  },
  {
    "id": "Alpha154",
    "no": 154,
    "name": "GTJA 154",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "volume",
      "vwap"
    ],
    "formula": "(((VWAP - MIN(VWAP, 16))) < (CORR(VWAP, MEAN(VOLUME, 180), 18)))"
  },
  {
    "id": "Alpha155",
    "no": 155,
    "name": "GTJA 155",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "SMA(VOLUME, 13, 2)-SMA(VOLUME, 27, 2)-SMA(SMA(VOLUME, 13, 2)-SMA(VOLUME, 27, 2), 10, 2)"
  },
  {
    "id": "Alpha156",
    "no": 156,
    "name": "GTJA 156",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "open",
      "low",
      "vwap"
    ],
    "formula": "(MAX(RANK(DECAYLINEAR(DELTA(VWAP, 5), 3)), RANK(DECAYLINEAR(((DELTA(((OPEN * 0.15) + (LOW *0.85)), 2) / ((OPEN * 0.15) + (LOW * 0.85))) * -1), 3))) * -1)"
  },
  {
    "id": "Alpha157",
    "no": 157,
    "name": "GTJA 157",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close",
      "returns"
    ],
    "formula": "(MIN(PROD(RANK(RANK(LOG(SUM(TSMIN(RANK(RANK((-1 * RANK(DELTA((CLOSE - 1), 5))))), 2), 1)))), 1), 5) + TSRANK(DELAY((-1 * RET), 6), 5))"
  },
  {
    "id": "Alpha158",
    "no": 158,
    "name": "GTJA 158",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "((HIGH-SMA(CLOSE, 15, 2))-(LOW-SMA(CLOSE, 15, 2)))/CLOSE"
  },
  {
    "id": "Alpha159",
    "no": 159,
    "name": "GTJA 159",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "((CLOSE-SUM(MIN(LOW, DELAY(CLOSE, 1)), 6))/SUM(MAX(HIGH, DELAY(CLOSE, 1))-MIN(LOW, DELAY(CLOSE, 1)), 6) *12*24+(CLOSE-SUM(MIN(LOW, DELAY(CLOSE, 1)), 12))/SUM(MAX(HIGH, DELAY(CLOSE, 1))-MIN(LOW, DELAY(CLOSE, 1)), 12)*6*24+(CLOSE-SUM(MIN(LOW, DELAY(CLOSE, 1)), 24))/SUM(MAX(HIGH, DELAY(CLOSE, 1))-MIN(LOW, DELAY(CLOSE, 1)), 24)*6*24)*100/(6*12+6*24+12*24)"
  },
  {
    "id": "Alpha160",
    "no": 160,
    "name": "GTJA 160",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA((CLOSE<=DELAY(CLOSE, 1)?STD(CLOSE, 20):0), 20, 1)"
  },
  {
    "id": "Alpha161",
    "no": 161,
    "name": "GTJA 161",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "MEAN(MAX(MAX((HIGH-LOW), ABS(DELAY(CLOSE, 1)-HIGH)), ABS(DELAY(CLOSE, 1)-LOW)), 12)"
  },
  {
    "id": "Alpha162",
    "no": 162,
    "name": "GTJA 162",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "(SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 12, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 12, 1)*100-MIN(SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 12, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 12, 1)*100, 12))/(MAX(SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 12, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 12, 1)*100, 12)-MIN(SMA(MAX(CLOSE-DELAY(CLOSE, 1), 0), 12, 1)/SMA(ABS(CLOSE-DELAY(CLOSE, 1)), 12, 1)*100, 12))"
  },
  {
    "id": "Alpha163",
    "no": 163,
    "name": "GTJA 163",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "high",
      "close",
      "volume",
      "vwap",
      "returns"
    ],
    "formula": "RANK(((((-1 * RET) * MEAN(VOLUME, 20)) * VWAP) * (HIGH - CLOSE)))"
  },
  {
    "id": "Alpha164",
    "no": 164,
    "name": "GTJA 164",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "SMA((((CLOSE>DELAY(CLOSE, 1))?1/(CLOSE-DELAY(CLOSE, 1)):1)-MIN(((CLOSE>DELAY(CLOSE, 1))?1/(CLOSE-DELAY(CLOSE, 1)):1), 12))/(HIGH-LOW)*100, 13, 2)"
  },
  {
    "id": "Alpha165",
    "no": 165,
    "name": "GTJA 165",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MAX(SUMAC(CLOSE-MEAN(CLOSE, 48)))-MIN(SUMAC(CLOSE-MEAN(CLOSE, 48)))/STD(CLOSE, 48)"
  },
  {
    "id": "Alpha166",
    "no": 166,
    "name": "GTJA 166",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "-20* （ 20-1 ） ^1.5*SUM(CLOSE/DELAY(CLOSE, 1)-1-MEAN(CLOSE/DELAY(CLOSE, 1)-1, 20), 20)/((20-1)*(20-2)(SUM((CLOSE/DELA Y(CLOSE, 1), 20)^2, 20))^1.5)"
  },
  {
    "id": "Alpha167",
    "no": 167,
    "name": "GTJA 167",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SUM((CLOSE-DELAY(CLOSE, 1)>0?CLOSE-DELAY(CLOSE, 1):0), 12)"
  },
  {
    "id": "Alpha168",
    "no": 168,
    "name": "GTJA 168",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "volume"
    ],
    "formula": "(-1*VOLUME/MEAN(VOLUME, 20))"
  },
  {
    "id": "Alpha169",
    "no": 169,
    "name": "GTJA 169",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA(MEAN(DELAY(SMA(CLOSE-DELAY(CLOSE, 1), 9, 1), 1), 12)-MEAN(DELAY(SMA(CLOSE-DELAY(CLOSE, 1), 9, 1), 1), 26), 10, 1)"
  },
  {
    "id": "Alpha170",
    "no": 170,
    "name": "GTJA 170",
    "theme": "vwap_deviation",
    "status": "ready",
    "fields": [
      "high",
      "close",
      "volume",
      "vwap"
    ],
    "formula": "((((RANK((1 / CLOSE)) * VOLUME) / MEAN(VOLUME, 20)) * ((HIGH * RANK((HIGH - CLOSE))) / (SUM(HIGH, 5) / 5))) - RANK((VWAP - DELAY(VWAP, 5))))"
  },
  {
    "id": "Alpha171",
    "no": 171,
    "name": "GTJA 171",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "open",
      "high",
      "low",
      "close"
    ],
    "formula": "((-1 * ((LOW - CLOSE) * (OPEN^5))) / ((CLOSE - HIGH) * (CLOSE^5)))"
  },
  {
    "id": "Alpha172",
    "no": 172,
    "name": "GTJA 172",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "formula_only"
    ],
    "formula": "MEAN(ABS(SUM((LD>0 & LD>HD)?LD:0, 14)*100/ SUM(TR, 14)-SUM((HD>0 & HD>LD)?HD:0, 14)*100/SUM(TR, 14))/(SUM((LD>0 & LD>HD)?LD:0, 14)*100/ SUM(TR, 14)+SUM((HD>0 & HD>LD)?HD:0, 14)*100/SUM(TR, 14))*100, 6)"
  },
  {
    "id": "Alpha173",
    "no": 173,
    "name": "GTJA 173",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "3*SMA(CLOSE, 13, 2)-2*SMA(SMA(CLOSE, 13, 2), 13, 2)+SMA(SMA(SMA(LOG(CLOSE), 13, 2), 13, 2), 13, 2);"
  },
  {
    "id": "Alpha174",
    "no": 174,
    "name": "GTJA 174",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "SMA((CLOSE>DELAY(CLOSE, 1)?STD(CLOSE, 20):0), 20, 1)"
  },
  {
    "id": "Alpha175",
    "no": 175,
    "name": "GTJA 175",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close"
    ],
    "formula": "MEAN(MAX(MAX((HIGH-LOW), ABS(DELAY(CLOSE, 1)-HIGH)), ABS(DELAY(CLOSE, 1)-LOW)), 6)"
  },
  {
    "id": "Alpha176",
    "no": 176,
    "name": "GTJA 176",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "CORR(RANK(((CLOSE - TSMIN(LOW, 12)) / (TSMAX(HIGH, 12) - TSMIN(LOW, 12)))), RANK(VOLUME), 6)"
  },
  {
    "id": "Alpha177",
    "no": 177,
    "name": "GTJA 177",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high"
    ],
    "formula": "((20-HIGHDAY(HIGH, 20))/20)*100"
  },
  {
    "id": "Alpha178",
    "no": 178,
    "name": "GTJA 178",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "(CLOSE-DELAY(CLOSE, 1))/DELAY(CLOSE, 1)*VOLUME"
  },
  {
    "id": "Alpha179",
    "no": 179,
    "name": "GTJA 179",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "low",
      "volume",
      "vwap"
    ],
    "formula": "(RANK(CORR(VWAP, VOLUME, 4)) *RANK(CORR(RANK(LOW), RANK(MEAN(VOLUME, 50)), 12)))"
  },
  {
    "id": "Alpha180",
    "no": 180,
    "name": "GTJA 180",
    "theme": "price_volume",
    "status": "ready",
    "fields": [
      "close",
      "volume"
    ],
    "formula": "((MEAN(VOLUME, 20) < VOLUME) ? ((-1 * TSRANK(ABS(DELTA(CLOSE, 7)), 60)) * SIGN(DELTA(CLOSE, 7)) : (-1 * VOLUME)))"
  },
  {
    "id": "Alpha181",
    "no": 181,
    "name": "GTJA 181",
    "theme": "benchmark_style",
    "status": "data_gap",
    "fields": [
      "benchmark_close",
      "close"
    ],
    "formula": "SUM(((CLOSE/DELAY(CLOSE, 1)-1)-MEAN((CLOSE/DELAY(CLOSE, 1)-1), 20))-(BENCHMARKINDEXCLOSE-MEAN(BENCHMARKINDEXCLOSE, 20))^2, 20)/SUM((BENCHMARKINDEXCLOSE-MEAN(BENCHMARKINDEXCLOSE, 20))^3)"
  },
  {
    "id": "Alpha182",
    "no": 182,
    "name": "GTJA 182",
    "theme": "benchmark_style",
    "status": "data_gap",
    "fields": [
      "benchmark_close",
      "benchmark_open",
      "open",
      "close"
    ],
    "formula": "COUNT((CLOSE>OPEN & BENCHMARKINDEXCLOSE>BENCHMARKINDEXOPEN)OR(CLOSE<OPEN & BENCHMARKINDEXCLOSE<BENCHMARKINDEXOPEN), 20)/20"
  },
  {
    "id": "Alpha183",
    "no": 183,
    "name": "GTJA 183",
    "theme": "volatility",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MAX(SUMAC(CLOSE-MEAN(CLOSE, 24)))-MIN(SUMAC(CLOSE-MEAN(CLOSE, 24)))/STD(CLOSE, 24)"
  },
  {
    "id": "Alpha184",
    "no": 184,
    "name": "GTJA 184",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "open",
      "close"
    ],
    "formula": "(RANK(CORR(DELAY((OPEN - CLOSE), 1), CLOSE, 200)) + RANK((OPEN - CLOSE)))"
  },
  {
    "id": "Alpha185",
    "no": 185,
    "name": "GTJA 185",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "open",
      "close"
    ],
    "formula": "RANK((-1 * ((1 - (OPEN / CLOSE))^2)))"
  },
  {
    "id": "Alpha186",
    "no": 186,
    "name": "GTJA 186",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "formula_only"
    ],
    "formula": "(MEAN(ABS(SUM((LD>0 & LD>HD)?LD:0, 14)*100/ SUM(TR, 14)-SUM((HD>0 & HD>LD)?HD:0, 14)*100/SUM(TR, 14))/(SUM((LD>0 & LD>HD)?LD:0, 14)*100/ SUM(TR, 14)+SUM((HD>0 & HD>LD)?HD:0, 14)*100/SUM(TR, 14))*100, 6)+DELAY(MEAN(ABS(SUM((LD>0 & LD>HD)?LD:0, 14)*100/SUM(TR, 14)-SUM((HD>0 & HD>LD)?HD:0, 14)*100/ SUM(TR, 14))/(SUM((LD>0 & LD>HD)?LD:0, 14)*100/SUM(TR, 14)+SUM((HD>0 & HD>LD)?HD:0, 14)*100/SUM(TR, 14))*100, 6), 6))/2"
  },
  {
    "id": "Alpha187",
    "no": 187,
    "name": "GTJA 187",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "open",
      "high"
    ],
    "formula": "SUM((OPEN<=DELAY(OPEN, 1)?0:MAX((HIGH-OPEN), (OPEN-DELAY(OPEN, 1)))), 20)"
  },
  {
    "id": "Alpha188",
    "no": 188,
    "name": "GTJA 188",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "high",
      "low"
    ],
    "formula": "((HIGH-LOW–SMA(HIGH-LOW, 11, 2))/SMA(HIGH-LOW, 11, 2))*100"
  },
  {
    "id": "Alpha189",
    "no": 189,
    "name": "GTJA 189",
    "theme": "price_structure",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "MEAN(ABS(CLOSE-MEAN(CLOSE, 6)), 6)"
  },
  {
    "id": "Alpha190",
    "no": 190,
    "name": "GTJA 190",
    "theme": "momentum_reversal",
    "status": "ready",
    "fields": [
      "close"
    ],
    "formula": "LOG((COUNT(CLOSE/DELAY(CLOSE)-1>((CLOSE/DELAY(CLOSE, 19))^(1/20)-1), 20)-1)*(SUMIF(((CLOSE/DELAY(CLOSE)-1-(CLOSE/DELAY(CLOSE, 19))^(1/20)-1))^2, 20, CLOSE/DELAY(CLOSE)-1<(CLOSE/DELAY(CLOSE, 19))^(1/20)- 1))/((COUNT((CLOSE/DELAY(CLOSE)-1<(CLOSE/DELAY(CLOSE, 19))^(1/20)-1), 20))*(SUMIF((CLOSE/DELAY(CLOSE)-1-((CLOSE/DELAY(CLOSE, 19))^(1/20)-1))^2, 20, CLOSE/DELAY(CLOSE)-1>(CLOSE/DELAY(CLOSE, 19))^(1/20)-1))))"
  },
  {
    "id": "Alpha191",
    "no": 191,
    "name": "GTJA 191",
    "theme": "price_volume_corr",
    "status": "ready",
    "fields": [
      "high",
      "low",
      "close",
      "volume"
    ],
    "formula": "((CORR(MEAN(VOLUME, 20), LOW, 5) + ((HIGH + LOW) / 2)) - CLOSE)"
  }
]
