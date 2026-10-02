import bot

print("LOADING NOBITEX 1m DATA...")

candles1m = bot.fetch_1m()

print("1M CANDLES:", len(candles1m))

candles3m = bot.make_3m(candles1m)

print("3M CANDLES:", len(candles3m))

signals = []

for i in range(257, len(candles3m)):
    result = bot.calculate_mexc(candles3m[:i+1])

    sig = result.get("signal")
    sl = result.get("stop_loss")

    if sig:
        signals.append({
            "bar": i,
            "time": candles3m[i][0],
            "signal": sig,
            "price": result["price"],
            "sl": sl,
            "ma5": result["ma5"],
            "ma8": result["ma8"],
            "ma256": result["ma256"],
        })

print()
print("HISTORICAL SIGNAL COUNT:", len(signals))
print()

for x in signals:
    print(
        f'BAR {x["bar"]} | '
        f'TIME {x["time"]} | '
        f'{x["signal"].upper()} | '
        f'PRICE {x["price"]:.8f} | '
        f'SL {x["sl"]:.8f} | '
        f'MA5 {x["ma5"]:.8f} | '
        f'MA8 {x["ma8"]:.8f} | '
        f'MA256 {x["ma256"]:.8f}'
    )
