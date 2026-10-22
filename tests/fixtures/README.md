# Original signal regression fixture

`legacy_market.csv.gz` contains 4,199 real sessions from 2010-02-11 through
2026-10-21. Prices are the Yahoo chart snapshots downloaded for the local audit.
Adjusted opens use each day's adjusted-close / close factor.

The `expected_signal` column was computed **before editing main.py**, by executing
only the original source's indicator and state-machine AST statements, from
repository commit `c0664b943eb6226aec1884e8f8821817b4652d32`.
SHA256 of the original main.py UTF-8 text:
`d3816d253ef8ad9a7b58cc243e34b5967895815a430e86c0d1ef05cbf5e886af`.

These warm-up signals deliberately match the original (QLD before MA170 is
available). They are excluded from executable backtest returns. The production
report also requires at least 201 validated sessions before publishing a target.

There are no credentials, PushPlus receipts, or account holdings in this fixture.
