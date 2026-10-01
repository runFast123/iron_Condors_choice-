"""The playground: replay a settled campaign with other settings, and plan
the campaign trading now by simulating where it can go.

Nothing here touches a live run or the user's saved backtest. Replays run
the same backtest engine on the campaign's own dates and expiry; plans run
the same ladder, fills, costs and settlement over simulated NIFTY paths.
"""
