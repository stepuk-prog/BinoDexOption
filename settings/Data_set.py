spr_timeframe = [{'timeframe': '1m', 'search_tf': '60', 'name_tf': '1 минута', 'coefficient': False},
                 {'timeframe': '3m', 'search_tf': '300', 'name_tf': '3 минуты', 'coefficient': True},
                 {'timeframe': '5m', 'search_tf': '300', 'name_tf': '5 минут', 'coefficient': False},
                 {'timeframe': '10m', 'search_tf': '900', 'name_tf': '10 минут', 'coefficient': True},
                 {'timeframe': '15m', 'search_tf': '900', 'name_tf': '15 минут', 'coefficient': False},
                 ]

t_option = [
    {'time': 185, 'name': '3 минуты'},
    {'time': 185, 'name': '3 минуты'},
    {'time': 245, 'name': '4 минуты'},
    {'time': 305, 'name': '5 минут'},
    {'time': 305, 'name': '5 минут'}]  # выбор времени сессий

find_timeframe = [
    {'timeframe': '1m', 'find': '1m'},
    {'timeframe': '3m', 'find': '1m'},
    {'timeframe': '5m', 'find': '5m'},
    {'timeframe': '10m', 'find': '5m'},
    {'timeframe': '15m', 'find': '15m'}]

# выбор времени догона, для закрытого канала
t_dogon = [2, 2, 2, 2, 1, 1]
