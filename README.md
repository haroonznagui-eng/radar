# radar

A personal fast-mover radar for US stocks. During the New York session it checks Yahoo Finance
once a minute and sends a phone notification (ntfy) when a stock matches `rules.json`:
a 5-minute candle up at least 1.5%, the stock up at least 6% on the day, at least twice its usual
volume, and price above VWAP.

Alerts only. It never places orders. Not financial advice.

In a backtest, this rule made money only with an instant entry; entering 5 minutes late lost money.
