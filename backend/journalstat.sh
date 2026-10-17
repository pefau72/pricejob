# price_job.py        →  prices         →  push_prices.py   →  /web/pricedata   →  elpriser.js
# weather_job.py      →  weather        →  push_weather.py  →  /web/weatherdata →  vejr.js
#                                          ↓
#                                    pushed_state + push_runs  →  notify.py
# -n number of lines

echo "Weathercollect:"
journalctl -u weathercollect.service   -n 3 --no-pager
echo "-"
journalctl -u weathercollect.timer --since "2 days ago"
echo "#####"

echo "Weatherpush:"
journalctl -u weatherpush.service      -n 3 --no-pager
echo "-"
journalctl -u weatherpush.timer    --since "2 days ago"
echo "#####"

echo "Elpriser:"
journalctl -u elpriser.service         -n 3 --no-pager
echo "-"
journalctl -u elpriser.timer       --since "2 days ago"
echo "#####"

echo "Pricepush:"
journalctl -u pricepush.service        -n 3 --no-pager
echo "-"
journalctl -u pricepush.timer      --since "2 days ago"
echo "#####"

echo "Jobs-notify:"
journalctl -u jobs-notify.service -n 3 --no-pager
echo "-"
journalctl -u jobs-notiy.timer     --since "2 days ago"
echo "-"




