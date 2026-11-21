import { $, empty, format, request } from './api.js';
import { raster } from './raster_motion.js';

export const localTerm = value => new Intl.DateTimeFormat('ru', { timeZone: 'Asia/Kamchatka',
  day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value));

function feature(coordinates, properties) {
  return { type: 'Feature', geometry: { type: 'Point', coordinates }, properties };
}

function windFeature(row, name, field) {
  const heading = row.wind_from_deg ?? row.wind_deg;
  const speed = row.wind_speed_ms ?? row.wind_speed;
  const gust = row.gust_ms ?? row.wind_gust;
  return feature(row.coordinates, {
    name,
    toward: (heading+180) % 360,
    speed,
    wind: heading == null || speed == null ? null : `Ветер ${format(speed)} м/с · откуда ${format(heading, 0)}°`,
    gust: gust == null ? null : `Порывы ${format(gust)} м/с`,
    reading: field?.value == null ? null : `${field.name} · ${format(field.value)} ${field.unit}`,
    distance: row.distance_to_source_m == null ? null : `До погодной точки ${format(row.distance_to_source_m/1000)} км`
  });
}

export function createWeatherLayers(map) {
  let dataset;
  let stamp;
  let catalog;
  let eventId;
  let atlas;
  let clouds;
  let forecastRevision;
  let result;
  let modelFrame;
  let token = 0;
  const field = $('atlas-field');
  const status = $('atlas-status');
  const display = () => field.value !== 'none';
  const sourceData = () => dataset === 'storm' ? catalog?.storms?.[eventId] : catalog?.[dataset];
  const menu = $('weather-menu');
  const tiles = $('weather-menu-tiles');
  const arrowsToggle = $('ops-wind-arrows');
  const cloudsToggle = $('atlas-clouds');

  function choose(next) {
    field.value = field.value === next ? 'none' : next;
    refresh();
  }

  function buildTiles(fields) {
    const displayFields = { none: { name: 'Без заливки', colors: ['#233b33', '#344c43'] }, ...fields };
    tiles.replaceChildren(...Object.entries(displayFields).map(([key, value]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'layer-tile';
      button.dataset.layer = key;
      const thumbnail = document.createElement('i');
      thumbnail.className = 'layer-preview';
      thumbnail.style.background = `linear-gradient(140deg,${value.colors.join(',')})`;
      thumbnail.setAttribute('aria-hidden', 'true');
      const name = document.createElement('span');
      name.textContent = value.name;
      button.append(thumbnail, name);
      button.addEventListener('click', () => choose(key));
      return button;
    }));
  }

  function legend() {
    $('scene-snow-legend').hidden = ['ops-scale-model', 'atlas-scale', 'ops-wind-legend'].every(id => $(id).hidden);
  }

  function paint() {
    for (const button of tiles.children) {
      button.setAttribute('aria-pressed', String(button.dataset.layer === field.value));
    }
    const showAtlas = atlas && display() && atlas.grid_cells > 0;
    map.setLayoutProperty('operations-atlas', 'visibility', showAtlas ? 'visible' : 'none');
    if (showAtlas) {
      raster(map, 'operations-atlas', atlas.bounds, atlas.overlay);
      const { name, colors, unit, range } = atlas.field;
      $('atlas-scale-label').textContent = name;
      $('atlas-scale-start').textContent = `${format(range[0], 0)} ${unit}`;
      $('atlas-scale-mid').textContent = format((range[0]+range[1])/2);
      $('atlas-scale-end').textContent = `${format(range[1], 0)} ${unit}`;
      $('atlas-scale-gradient').style.background = `linear-gradient(90deg,${colors.join(',')})`;
    }
    $('atlas-scale').hidden = !showAtlas;
    const showClouds = Boolean(cloudsToggle.checked && clouds?.grid_cells);
    map.setLayoutProperty('operations-clouds', 'visibility', showClouds ? 'visible' : 'none');
    if (showClouds) raster(map, 'operations-clouds', clouds.bounds, clouds.overlay);
    const weather = result?.wind_frames?.[modelFrame ?? 0];
    const wind = weather?.points || atlas?.points || [];
    const arrows = arrowsToggle.checked
      ? wind.filter(p => (p.wind_speed_ms ?? p.wind_speed) > .2 && (p.wind_from_deg ?? p.wind_deg) != null)
        .map(p => windFeature(p, weather ? 'Ветер на входе модели' : atlas.source))
      : [];
    const points = $('atlas-points').checked && atlas
      ? atlas.points.map(p => windFeature(p, atlas.source, { ...atlas.field, value: p[field.value] })) : [];
    map.getSource('weather-arrows').setData({ ...empty(), features: arrows });
    map.getSource('weather-points').setData({ ...empty(), features: points });
    $('ops-wind-legend').hidden = !arrows.length;
    legend();
  }

  async function refresh() {
    const epoch = ++token;
    if (!dataset || !stamp || !sourceData()) {
      atlas = undefined;
      clouds = undefined;
      status.textContent = '';
      paint();
      return;
    }
    const name = display() ? field.value : 'wind_speed';
    const params = new URLSearchParams({
      dataset, valid_at: stamp, field: name, method: $('atlas-method').value
    });
    if (eventId) params.set('event_id', eventId);
    if (forecastRevision && dataset === 'live') params.set('revision', forecastRevision);
    const haze = new URLSearchParams(params);
    haze.set('field', 'clouds');
    haze.set('haze', 'true');
    const cloudUpdate = cloudsToggle.checked && sourceData().fields.clouds
      ? request(`/operations/atlas/frame?${haze}`).then(frame => {
        if (epoch !== token) return;
        clouds = frame;
        paint();
      }).catch(() => {
        if (epoch !== token) return;
        clouds = undefined;
        paint();
      })
      : Promise.resolve();
    try {
      const frame = await request(`/operations/atlas/frame?${params}`);
      if (epoch !== token) return;
      atlas = frame;
      status.textContent = `${frame.source} · ${localTerm(frame.valid_at)} · ${frame.points.length} точек` +
        (display() && !frame.grid_cells ? ' · значений слоя нет' : '');
      paint();
    } catch (error) {
      if (epoch !== token) return;
      atlas = undefined;
      status.textContent = error.message;
      paint();
    }
    await cloudUpdate;
  }

  field.addEventListener('change', refresh);
  cloudsToggle.addEventListener('change', () => {
    paint();
    refresh();
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  document.addEventListener('pointerdown', event => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  $('atlas-method').addEventListener('change', refresh);
  for (const id of ['ops-wind-arrows', 'atlas-points']) $(id).addEventListener('change', paint);
  $('atlas-opacity').addEventListener('input', event => {
    map.setPaintProperty('operations-atlas', 'raster-opacity', event.target.valueAsNumber/100);
  });
  map.setPaintProperty('operations-atlas', 'raster-opacity', $('atlas-opacity').valueAsNumber/100);

  return {
    setCatalog(value) { catalog = value; },
    setWeather(source, timestamp, storm, revision = catalog?.live?.sha256) {
      const sourceChanged = source !== dataset || storm !== eventId;
      if (sourceChanged) atlas = clouds = undefined;
      [dataset, stamp, eventId] = [source, timestamp, storm];
      forecastRevision = revision;
      if (sourceChanged) {
        const previous = field.value;
        const options = sourceData()?.fields || {};
        field.replaceChildren(new Option('Выключен', 'none'), ...Object.entries(options)
          .map(([key, item]) => new Option(item.name, key)));
        field.value = previous in options ? previous : options.snow ? 'snow' : 'none';
        buildTiles(options);
      }
      paint();
      $('atlas-clouds-row').hidden = !sourceData()?.fields.clouds;
      $('atlas-display-settings').hidden = !source;
      refresh();
    },
    setResult(value) {
      result = value;
      modelFrame = 0;
      paint();
    },
    setFrame(index) { modelFrame = index; paint(); },
    legend
  };
}
