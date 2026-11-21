"""Normalize atlas responses and define the fixed visual encoding."""
from collections import Counter, defaultdict
from contextlib import closing
import csv
from datetime import datetime
import json
import shutil
from zoneinfo import ZoneInfo

from .experiment import coordinate_key, iso, numeric
from .heatmap_collect import OUT, RUN, ledger, point_plan, retained_responses

LOCAL = ZoneInfo("Asia/Kamchatka")
from .heatmap_fields import FIELDS, shown_value

EXPORT = [
    "point_id","lat","lon","source_product","round","request_time_utc","forecast_dt_utc",
    "local_time_kamchatka","temp","feels_like","wind_speed","wind_gust","wind_deg","snow_3h",
    "rain_3h","current_snow_mm_h","current_rain_mm_h","visibility","humidity","clouds","pop",
    "precip_intensity_mm_h","weather_code","api_lat","api_lon","api_city_id","raw_file",
]
def local_time(epoch):
    return datetime.fromtimestamp(epoch, LOCAL)


def export_csv(filename, columns, rows):
    with (OUT / filename).open("w",encoding="utf-8",newline="") as file:
        writer=csv.DictWriter(file,columns,extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize(payload, point, product, round_id, request_time, raw_file):
    is_forecast = product=="forecast"
    site = payload.get("city",{}) if is_forecast else payload
    for item in payload.get("list",[]) if is_forecast else [payload]:
        epoch = item.get("dt")
        if not isinstance(epoch,int):
            continue
        main = item.get("main",{})
        wind = item.get("wind",{})
        snow = numeric(item.get("snow",{}).get("3h" if is_forecast else "1h"))
        rain = numeric(item.get("rain",{}).get("3h" if is_forecast else "1h"))
        yield dict(point_id=point["id"],lat=point["latitude"],lon=point["longitude"],
                   source_product="Forecast" if is_forecast else "Current",round=round_id,
                   request_time_utc=iso(request_time),forecast_dt_utc=iso(epoch),epoch=epoch,
                   local_time_kamchatka=local_time(epoch).isoformat(), temp=numeric(main.get("temp")),
                   feels_like=numeric(main.get("feels_like")),wind_speed=numeric(wind.get("speed")),
                   wind_gust=numeric(wind.get("gust")),wind_deg=numeric(wind.get("deg")),
                   snow_3h=snow if is_forecast else None,rain_3h=rain if is_forecast else None,
                   current_snow_mm_h=None if is_forecast else snow,
                   current_rain_mm_h=None if is_forecast else rain,
                   visibility=numeric(item.get("visibility")),humidity=numeric(main.get("humidity")),
                   clouds=numeric(item.get("clouds",{}).get("all")),pop=numeric(item.get("pop")),
                   precip_intensity_mm_h=(snow+rain)/3 if is_forecast and snow is not None and rain is not None
                   else None, weather_code=(item.get("weather") or [{}])[0].get("id"),
                   api_lat=numeric(site.get("coord",{}).get("lat")),
                   api_lon=numeric(site.get("coord",{}).get("lon")),api_city_id=numeric(site.get("id")),
                   raw_file=raw_file)


def loaded():
    plan=point_plan()
    points={coordinate_key(point):point for point in plan}
    with closing(ledger()) as db:
        receipts=retained_responses(db)
        attempts=dict(db.execute("SELECT status, COUNT(*) FROM attempts GROUP BY status"))
    records=[]
    archive=[]
    seen=set()
    for round_id, product, coord, epoch, path in sorted(receipts,key=lambda item:-item[3]):
        if coord not in points or (product,coord) in seen:
            continue
        seen.add((product,coord))
        dest=OUT/"raw"/product/round_id/path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if path.resolve()!=dest.resolve() and not dest.exists():
            shutil.copyfile(path,dest)
        raw_file=str(dest.relative_to(OUT))
        archive.append(dict(round=round_id,product=product,coordinate=coord,request_epoch=epoch,
                            request_time_utc=iso(epoch),raw_file=raw_file))
        records.extend(normalize(json.loads(dest.read_bytes()),points[coord],product,round_id,epoch,raw_file))
    forecast=defaultdict(dict)
    for record in records:
        if record["source_product"]=="Forecast":
            forecast[record["point_id"]][record["epoch"]]=record
    if not forecast:
        raise ValueError("Нет сохранённых ответов Forecast.")
    # Every frame uses the same requested points and common exact valid time.
    valid_times=sorted(set.intersection(*(set(rows) for rows in forecast.values())))
    if not valid_times:
        raise ValueError("У всех сохранённых точек нет общего срока действия.")
    frames=[
        {**forecast[p["id"]][date],"frame_index":index}
        for index,date in enumerate(valid_times) for p in plan if p["id"] in forecast
    ]
    export_csv("raw_index.csv",["round","product","coordinate","request_epoch","request_time_utc","raw_file"],archive)
    export_csv("observations_full.csv",EXPORT,sorted(records,key=lambda r:(r["epoch"],r["point_id"])))
    export_csv("forecast_frames.csv",["frame_index",*EXPORT],frames)
    export_csv("point_metadata.csv",
               ["point_id","lat","lon","atlas_kind","district","terrain","inside_city","on_land",
                "cell_500m","elevation_m_dsm","elevation_source","osm","road","forecast_received",
                "current_received"],
               [{**p,"point_id":p["id"],"lat":p["latitude"],"lon":p["longitude"],
                 "cell_500m":p["cell_ids"]["500"],"forecast_received":("forecast",coord) in seen,
                 "current_received":("weather",coord) in seen} for coord,p in points.items()])
    plan=[{**point,"received":point["id"] in forecast} for point in plan]
    statistics={
        "attempts":attempts,"planned":len(plan),"received":len(forecast),
        "old_forecast":len({r["point_id"] for r in records if r["source_product"]=="Forecast" and r["round"]!=RUN}),
        "by_kind":dict(Counter(p["atlas_kind"] for p in plan if p["received"])),
        "planned_by_kind":dict(Counter(p["atlas_kind"] for p in plan)),
        "response_coord_count":len({(rows[valid_times[0]]["api_lat"],rows[valid_times[0]]["api_lon"]) for rows in forecast.values()}),
        "full_profile_count":len({tuple(shown_value(rows[date],f) for date in valid_times for f in FIELDS)
                                  for rows in forecast.values()}),
        "first_request":min(r["request_time_utc"] for r in frames),
        "last_request":max(r["request_time_utc"] for r in frames),
        "local_start":local_time(valid_times[0]).isoformat(),
        "local_end":local_time(valid_times[-1]).isoformat(),
        "frame_count":len(valid_times),
    }
    return plan, valid_times, frames, statistics
