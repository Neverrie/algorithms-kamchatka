"""Shared atlas field names, scales and display units."""

# Fixed across time, maps and video. The display percentage is computed in parsing.
FIELDS = {
    "temp": ("Температура", "°C", (-13, 5), ["#362869","#536cc4","#8fd9df","#f6d483","#dd7145"]),
    "feels_like": ("Ощущаемая температура", "°C", (-20, 4), ["#362869","#536cc4","#8fd9df","#f6d483","#dd7145"]),
    "wind_speed": ("Скорость ветра", "м/с", (0, 12), ["#215766","#51b9a6","#d9e477","#f79b58","#c65163"]),
    "wind_gust": ("Порывы ветра", "м/с", (0, 20), ["#215766","#51b9a6","#d9e477","#f79b58","#c65163"]),
    "wind_deg": ("Направление ветра", "°", (0, 360), ["#ecb45f","#dc6f90","#7e78cd","#49aba8","#ecb45f"]),
    "snow": ("Снег за 3 часа", "мм / 3 ч", (0, 7), ["#265365","#58b9ca","#a5dfef","#ccb8fb","#a47fdf"]),
    "rain": ("Дождь за 3 часа", "мм / 3 ч", (0, 7), ["#1d5264","#2599aa","#8de0bc","#ebdc83","#ee7955"]),
    "visibility": ("Видимость", "км", (0, 10), ["#e36e62","#f3b882","#dce6b0","#72c9c2","#3e8bab"]),
    "humidity": ("Влажность", "%", (45, 100), ["#e78e61","#e2be77","#91d2b6","#56aabf","#797de2"]),
    "clouds": ("Облачность", "%", (0, 100), ["#304a77","#5488ae","#77b9cf","#b2d0e0","#ecedf9"]),
    "pop": ("Вероятность осадков", "%", (0, 100), ["#1f4a69","#3798b2","#80d4c3","#efd980","#e88259"]),
    "precip": ("Суммарная интенсивность", "мм/ч", (0, 3), ["#275470","#52bfd1","#b0e7ef","#e9b0fd","#b487e7"]),
}
SOURCE_FIELDS = {
    "snow":"snow_3h","rain":"rain_3h","precip":"precip_intensity_mm_h",
}


def shown_value(row, field):
    value=row[SOURCE_FIELDS.get(field,field)]
    if value is None:
        return None
    if field=="visibility":
        return value/1000
    if field=="pop":
        return value*100
    return value
