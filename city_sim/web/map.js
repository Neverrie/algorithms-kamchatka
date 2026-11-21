import { addMotionSource } from './raster_motion.js';
import { $, empty, format, request } from './api.js';

export const areas = [
  { id: 'center', label: 'Центр · Култучное озеро', center: [158.642, 53.0334],
    camera: [158.642, 53.034], zoom: 13.55, region: [158.628, 53.018, 158.658, 53.047] },
  { id: 'north', label: 'Северо-Восток', center: [158.663, 53.075],
    zoom: 13.55, region: [158.645, 53.061, 158.684, 53.09] },
  { id: 'seroglazka', label: 'Сероглазка', center: [158.618, 53.053],
    zoom: 13.8, region: [158.601, 53.039, 158.633, 53.066] },
  { id: 'south', label: 'Завойко', center: [158.671, 52.959],
    zoom: 13.5, region: [158.643, 52.944, 158.704, 52.977] }
];

const HEIGHT_NAMES = {
  osm_height: 'Высота из OpenStreetMap',
  estimated_from_levels_3m_per_level: 'Оценка по этажности OSM',
  estimated_from_register_floors: 'Оценка по этажности городского реестра',
  estimated_from_gba: 'Модельная высота GlobalBuildingAtlas',
  unknown: 'Высота неизвестна'
};
const ASSET_NAMES = {
  hospital: 'Больница', clinic: 'Клиника', school: 'Школа', kindergarten: 'Детский сад',
  police: 'Полиция', fire_station: 'Пожарная часть', substation: 'Подстанция',
  bus_station: 'Автостанция', university: 'Университет'
};
const SLOPE_COLORS = ['interpolate', ['linear'], ['abs', ['to-number', ['get', 'slope_pct'], 0]],
  0, '#aadead', 6, '#f7db87', 12, '#f2996e', 24, '#f27070'];
const LIGHT = { anchor: 'map', color: '#fff1da', intensity: 0.42, position: [1.5, 310, 48] };
const LABELS = [
  ['Авачинская бухта', [158.616, 53.032], 'water', 0],
  ['Култучное озеро', [158.6509, 53.0314], 'water', 12.9],
  ['Мишенная сопка', [158.640, 53.046], '', 12.9],
  ['Никольская сопка', [158.640, 53.0195], '', 13.4],
  ['Сероглазка', [158.6157, 53.0535], '', 12.5],
  ['Северо-Восток', [158.6636, 53.076], '', 12.5],
  ['Завойко', [158.683, 52.962], '', 12]
];

function band(kind, bounds) {
  return { type: kind === 'dem' ? 'raster-dem' : 'raster', tileSize: 256,
    bounds, minzoom: 8, maxzoom: kind === 'dem' ? 14 : 15,
    tiles: [location.origin + `/api/v1/tiles/${kind}/{z}/{x}/{y}.png`] };
}

function popup(map, coordinates, title, lines, sources = []) {
  const content = document.createElement('div');
  content.className = 'popup-details';
  const heading = document.createElement('b');
  heading.textContent = title;
  content.append(heading);
  for (const line of lines) {
    const row = document.createElement('span');
    row.textContent = line;
    content.append(row);
  }
  for (const [label, url] of sources) {
    const link = document.createElement('a');
    link.textContent = label;
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    content.append(link);
  }
  new maplibregl.Popup({ maxWidth: '290px' }).setLngLat(coordinates).setDOMContent(content).addTo(map);
}

function installInteraction(map, buildingEvidence, snowDetails) {
  let popupRequest = 0;
  map.on('click', async event => {
    if (map.getCanvas().dataset.drawing === 'true') return;
    const token = ++popupRequest;
    const first = map.queryRenderedFeatures(event.point, { layers: ['structures', 'facilities', 'network', 'grades', 'topo-check', 'candidate-outline', 'streets', 'arterials', 'weather-arrows', 'weather-points'] })[0];
    const props = first?.properties;
    const where = event.lngLat;
    const snow = await snowDetails(where, first?.layer.id === 'structures' || Boolean(props?.edge_id));
    if (token !== popupRequest) return;
    if (first?.layer.id === 'weather-arrows' || first?.layer.id === 'weather-points') {
      popup(map, where, props.name, [props.reading, props.wind, props.gust, props.distance, ...snow].filter(Boolean));
    } else if (first?.layer.id === 'structures') {
      const lines = [HEIGHT_NAMES[props.height_source]];
      const sources = [];
      if (props.height_source === 'unknown') lines.push(`Условная высота на карте · ${format(props.render_height_m)} м`);
      else lines.push(`Высота на карте · ${format(props.height_m)} м`);
      if (props.height_low_m != null) {
        lines.push(`Оценочный диапазон · ${format(props.height_low_m)}–${format(props.height_high_m)} м`);
      }
      if (props.address && props.name) lines.push(props.address);
      if (props.construction_year) lines.push(`Год постройки · ${props.construction_year}`);
      if (props.registered_floors) lines.push(`Этажей в реестре · ${props.registered_floors}`);
      if (props.levels) lines.push(`Этажей в OSM · ${format(props.levels)}`);
      if (props.model_height_m != null && props.height_source !== 'estimated_from_gba') {
        lines.push(`Оценка GlobalBuildingAtlas · ${format(props.model_height_m)} м`);
      }
      if (props.location_overlap_iou != null) {
        lines.push(`Контур по снимку · пересечение ${format(props.location_overlap_iou * 100, 0)}% по IoU`);
        lines.push(`Смещение центров · ${format(props.location_center_offset_m)} м`);
      }
      if (props.data_kind === 'osm_via_overture') lines.push('Контур OSM из выпуска Overture');
      if (props.height_flags && props.height_flags !== '[]') lines.push('Есть расхождения в сведениях о здании');
      for (const [label, source, page] of [
        ['Источник этажности', props.registered_floors_source, props.registered_floors_page],
        ['Источник года постройки', props.construction_year_source, props.construction_year_page]
      ]) {
        const url = buildingEvidence.sources?.[source]?.url;
        if (url && page) sources.push([label, `${url}#page=${page}`]);
      }
      if (props.model_height_m != null || props.location_source) {
        const url = buildingEvidence.location_check?.url;
        if (url) sources.push(['GlobalBuildingAtlas', url]);
      }
      popup(map, where, props.name || props.address || 'Здание',
        [...snow, ...lines, props.asset_id], sources);
    } else if (first?.layer.id === 'facilities') {
      popup(map, where, props.name || ASSET_NAMES[props.kind] || 'Объект инфраструктуры',
        [ASSET_NAMES[props.kind] || props.kind, props.object_id]);
    } else if (first?.layer.id === 'topo-check') {
      popup(map, where, props.name, [
        `Топографический лист · ${format(props.map_m)} м`,
        `Copernicus GLO‑30 · ${format(props.dsm_m)} м`,
        `Разность DSM − карта · ${format(props.delta_m)} м`,
        `В радиусе 60 м · ${format(props.nearby_min_m)}…${format(props.nearby_max_m)} м`,
        'N‑57‑102 · издание 1990 · местность 1987'
      ]);
    } else if (first?.layer.id === 'candidate-outline') {
      popup(map, where, 'Кандидат по снимку', ['GlobalBuildingAtlas · требуется ручная проверка контура']);
    } else if (first?.layer.id === 'streets' || first?.layer.id === 'arterials') {
      popup(map, where, props.name || 'Дорога OpenStreetMap', ['Картографическая линия OSM', first.id]);
    } else if (props?.edge_id) {
      const direction = map.queryRenderedFeatures(event.point, { layers: ['network', 'grades'] })
        .filter(f => f.properties.physical_road_id === props.physical_road_id);
      const sameRoad = [...new Map(direction.map(f => [f.properties.edge_id, f.properties])).values()];
      const lines = sameRoad.map(edge => edge.slope_pct == null ? edge.edge_id : `${edge.edge_id} · ${format(edge.slope_pct)}%`);
      popup(map, where, props.road_name || 'Участок дороги', [
        ...snow, `Длина · ${format(props.length_m, 0)} м`, ...lines,
        ...(props.network_extension ? ['Расширение графа из OpenStreetMap · зимний допуск не проверен'] : []),
        ...(props.relief_quality === 'shorter_than_grid' ? ['Дуга короче шага DSM'] : [])
      ]);
    } else if (snow.length) {
      popup(map, where, 'Снег в этой точке', snow);
    } else {
      try {
        const info = await request(`/city/elevation?lon=${where.lng}&lat=${where.lat}`);
        const lines = info.elevation_m == null ? ['Точка вне покрытия'] :
          [`${format(info.elevation_m)} м · EGM2008`, 'Copernicus GLO‑30 · высота поверхности в ячейке DSM'];
        popup(map, where, 'Высота поверхности', lines);
      } catch (error) {
        $('scene-status').textContent = error.message;
      }
    }
  });
}

function weatherArrow() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 40;
  const ctx = canvas.getContext('2d');
  ctx.beginPath();
  ctx.moveTo(20, 33);
  ctx.lineTo(20, 8);
  ctx.moveTo(10, 18);
  ctx.lineTo(20, 8);
  ctx.lineTo(30, 18);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.strokeStyle = '#132026';
  ctx.lineWidth = 8;
  ctx.stroke();
  ctx.strokeStyle = '#ecffff';
  ctx.lineWidth = 4;
  ctx.stroke();
  return ctx.getImageData(0, 0, 40, 40);
}

function installDataLoading(map, metadata) {
  const step = 0.025;
  const cache = new Map();
  let generation = 0;
  const layers = { buildings: 'buildings', roads: 'roads', assets: 'infrastructure', candidates: 'building-candidates' };

  function getCell(layer, x, y) {
    const key = `${layer}/${x}/${y}`;
    if (cache.has(key)) return cache.get(key);
    const bbox = [x, y, x + 1, y + 1].map(index => (step * index).toFixed(3));
    const query = layer === 'roads' ? '&network=city' : '';
    const pending = request(`/city/${layer}?bbox=${bbox.join(',')}${query}`).catch(error => {
      cache.delete(key);
      throw error;
    });
    cache.set(key, pending);
    if (cache.size > 150) cache.delete(cache.keys().next().value);
    return pending;
  }

  async function load() {
    const token = ++generation;
    const point = map.getCenter();
    $('coords').textContent = `${point.lat.toFixed(4)}° с. ш. · ${point.lng.toFixed(4)}° в. д.`;
    const box = map.getBounds().toArray().flat();
    const [west, south, east, north] = metadata.bbox;
    const cells = [];
    const zoom = map.getZoom();
    if (zoom >= 12.6) {
      for (let x = Math.floor(Math.max(box[0], west) / step); x <= Math.floor(Math.min(box[2], east) / step); x++) {
        for (let y = Math.floor(Math.max(box[1], south) / step); y <= Math.floor(Math.min(box[3], north) / step); y++) cells.push([x, y]);
      }
    }
    try {
      await Promise.all(Object.entries(layers).map(async ([source, layer]) => {
        const enabled = $(source).checked || (source === 'roads' && $('slopes').checked);
        const requests = enabled ? cells.map(([x, y]) => getCell(layer, x, y)) : [];
        const chunks = await Promise.all(requests);
        if (token !== generation) return;
        const features = new Map();
        for (const chunk of chunks) {
          for (const f of chunk.features) features.set(f.id, f);
        }
        map.getSource(source).setData({ type: 'FeatureCollection', features: [...features.values()] });
      }));
      if (token === generation) {
        $('scene-status').textContent = zoom < 12.6 ? 'Приблизьте карту, чтобы увидеть здания и дуги' : '';
      }
    } catch (error) {
      if (token === generation) $('scene-status').textContent = `Слои · ${error.message}`;
    }
  }
  map.on('moveend', load);
  return load;
}

export function createMap(metadata, onAreaChange, snowDetails) {
  const { bbox } = metadata;
  const [first] = areas;
  const map = new maplibregl.Map({
    container: 'map', center: first.camera, zoom: first.zoom, pitch: 57, bearing: -23,
    touchPitch: true, cooperativeGestures: false,
    minZoom: 10.5, maxZoom: 19, maxPitch: 72, maxBounds: [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
    canvasContextAttributes: { antialias: true }, attributionControl: false,
    style: {
      version: 8, light: LIGHT, sources: {},
      layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#35606a' } }]
    }
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 100, unit: 'metric' }), 'bottom-left');
  map.addControl(new maplibregl.AttributionControl({ compact: true, customAttribution: [
    '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a>',
    ...(metadata.terrain ? ['<a href="https://registry.opendata.aws/copernicus-dem/">Copernicus WorldDEM‑30</a>'] : [])
  ] }));
  if (metadata.terrain) {
    const notice = document.createElement('span');
    notice.className = 'terrain-attribution';
    notice.textContent = metadata.terrain.attribution;
    $('metadata').append(notice);
  }
  let area = first;
  const viewButton = $('view');
  function updatePitch() {
    const pitch = map.getPitch();
    viewButton.dataset.pitch = pitch.toFixed(1);
    viewButton.textContent = pitch > 1 ? '3D' : '2D';
    viewButton.setAttribute('aria-pressed', String(pitch > 1));
  }
  map.on('pitch', updatePitch);
  const mapContainer = map.getContainer();
  let trackpadUntil = 0;
  mapContainer.addEventListener('wheel', event => {
    if (event.ctrlKey || event.metaKey) return;
    const vertical = Math.abs(event.deltaY);
    const now = performance.now();
    const pixelScroll = event.deltaMode === WheelEvent.DOM_DELTA_PIXEL &&
      (vertical < 100 || now < trackpadUntil) && vertical >= Math.abs(event.deltaX);
    if (!vertical || (!event.altKey && !pixelScroll)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (pixelScroll) trackpadUntil = now + 160;
    const delta = Math.max(-55, Math.min(55, event.deltaY));
    map.setPitch(Math.max(0, Math.min(map.getMaxPitch(), map.getPitch() + delta * .22)));
  }, { capture: true, passive: false });
  updatePitch();

  const areaSelect = $('area');
  areaSelect.replaceChildren(...areas.map(item => new Option(item.label, item.id)));
  function locate(item, moved) {
    areaSelect.value = item.id ?? '';
    const center = item.camera || item.center;
    $('scene-location').textContent = item.label;
    map.easeTo({ center, zoom: item.zoom || 14, pitch: map.getPitch(), bearing: map.getBearing(), duration: 900 });
    area = item;
    if (moved) onAreaChange(item);
  }
  areaSelect.addEventListener('change', () => locate(areas.find(item => item.id === areaSelect.value), true));
  $('focus').addEventListener('click', () => locate(area, false));
  $('view').addEventListener('click', () => {
    const use3D = map.getPitch() <= 1;
    map.easeTo({ pitch: use3D ? 57 : 0, bearing: use3D ? -23 : 0, duration: 650 });
  });

  const ready = new Promise(resolve => map.once('load', () => {
    if (metadata.terrain) {
      map.addSource('dem', band('dem', bbox));
      map.addSource('shade-dem', band('dem', bbox));
      map.addSource('relief', band('relief', bbox));
      map.addLayer({ id: 'relief-color', type: 'raster', source: 'relief', paint: { 'raster-fade-duration': 0 } });
      map.addLayer({ id: 'hillshade', type: 'hillshade', source: 'shade-dem', paint: {
        'hillshade-exaggeration': 0.57, 'hillshade-shadow-color': '#253934',
        'hillshade-highlight-color': '#f8e2b5', 'hillshade-accent-color': '#387776',
        'hillshade-illumination-direction': 315, 'hillshade-illumination-anchor': 'map'
      } });
      map.setTerrain({ source: 'dem', exaggeration: 1 });
    } else {
      $('terrain').checked = false;
      $('terrain').disabled = true;
    }

    map.addSource('base', { type: 'geojson', data: '/api/v1/city/base' });
    map.addLayer({ id: 'green', type: 'fill', source: 'base', filter: ['==', 'kind', 'green'],
      paint: { 'fill-color': '#486b53', 'fill-opacity': .24 } });
    map.addLayer({ id: 'water', type: 'fill', source: 'base', filter: ['==', 'kind', 'water'],
      paint: { 'fill-color': '#244f60', 'fill-opacity': .97 } });
    map.addLayer({ id: 'shore', type: 'line', source: 'base', filter: ['==', 'kind', 'water'],
      paint: { 'line-color': '#93bcc0', 'line-opacity': .55, 'line-width': 1 } });
    map.addLayer({ id: 'streets', type: 'line', source: 'base', filter: ['==', 'kind', 'street'], minzoom: 12,
      paint: { 'line-color': '#d3c9b2', 'line-opacity': .45,
        'line-width': ['interpolate', ['linear'], ['zoom'], 12, .6, 16, 1.6] } });
    map.addLayer({ id: 'arterials', type: 'line', source: 'base', filter: ['==', 'kind', 'arterial'],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#e1cea0', 'line-opacity': .65,
        'line-width': ['interpolate', ['linear'], ['zoom'], 11, .7, 17, 3] } });

    for (const id of ['buildings', 'roads', 'assets', 'scenario-boundary', 'audit', 'candidates']) {
      map.addSource(id, { type: 'geojson', data: empty() });
    }
    for (const id of ['operations-atlas', 'operations-snow', 'operations-clouds']) {
      addMotionSource(map, id);
    }
    map.addLayer({ id: 'operations-atlas', type: 'raster', source: 'operations-atlas',
      layout: { visibility: 'none' }, paint: { 'raster-opacity': .48, 'raster-fade-duration': 0 } });
    map.addLayer({ id: 'operations-snow', type: 'raster', source: 'operations-snow',
      layout: { visibility: 'none' }, paint: { 'raster-opacity': .82, 'raster-fade-duration': 0,
        'raster-resampling': 'linear' } });
    map.addLayer({ id: 'operations-clouds', type: 'raster', source: 'operations-clouds',
      layout: { visibility: 'none' }, paint: { 'raster-opacity': .48, 'raster-fade-duration': 0 } });
    map.addLayer({ id: 'network', type: 'line', source: 'roads', layout: { 'line-cap': 'round' },
      paint: { 'line-color': ['case', ['==', ['get', 'network_extension'], true], '#8cc9ce',
        ['match', ['get', 'road_class'], ['primary', 'secondary', 'tertiary'], '#fce1a0', '#dbccaa']],
        'line-opacity': .71, 'line-width': ['interpolate', ['linear'], ['zoom'], 12.6, .6, 17, 2.5] } });
    map.addLayer({ id: 'grades', type: 'line', source: 'roads', layout: { visibility: 'none' },
      filter: ['!=', ['get', 'slope_pct'], null], paint: { 'line-color': SLOPE_COLORS, 'line-width': 3.2 } });
    map.addLayer({ id: 'candidate-outline', type: 'line', source: 'candidates', layout: { visibility: 'none' },
      paint: { 'line-color': '#8de4ff', 'line-width': 1.5, 'line-dasharray': [2, 2] } });
    map.addLayer({ id: 'structures', type: 'fill-extrusion', source: 'buildings',
      paint: {
        'fill-extrusion-color': ['match', ['get', 'height_source'], 'osm_height', '#f1dca9',
          'estimated_from_register_floors', '#b9d8a6',
          'estimated_from_levels_3m_per_level', '#c6a994', 'estimated_from_gba', '#9ebdcb', '#8fa59c'],
        'fill-extrusion-height': ['get', 'render_height_m'], 'fill-extrusion-base': 0,
        'fill-extrusion-opacity': 1, 'fill-extrusion-vertical-gradient': true
      } });
    map.addLayer({ id: 'facilities', type: 'circle', source: 'assets', layout: { visibility: 'none' },
      paint: { 'circle-color': '#80f5c4', 'circle-radius': 4.5,
        'circle-stroke-width': 1.5, 'circle-stroke-color': '#194337' } });
    map.addLayer({ id: 'scenario-box', type: 'line', source: 'scenario-boundary',
      paint: { 'line-color': '#cdf69a', 'line-width': 2, 'line-dasharray': [3, 3] } });
    map.addLayer({ id: 'topo-check', type: 'circle', source: 'audit', layout: { visibility: 'none' },
      paint: { 'circle-color': '#d8afff', 'circle-radius': 6, 'circle-stroke-width': 2, 'circle-stroke-color': '#2d1846' } });
    map.addSource('operations-roads', { type: 'geojson', data: empty() });
    map.addLayer({ id: 'operations-roads', type: 'line', source: 'operations-roads',
      paint: { 'line-color': '#ffbd73', 'line-width': 2.5, 'line-opacity': .81 } }, 'network');
    map.addSource('operations-points', { type: 'geojson', data: empty() });
    map.addLayer({ id: 'operations-points', type: 'circle', source: 'operations-points',
      paint: { 'circle-color': ['match', ['get', 'status'], 'inspect', '#ffca80',
        'missing', '#9eabb0', 'priority', '#fff3c4', 'extrapolated', '#c2a6ec', '#a8d8b2'],
        'circle-radius': ['match', ['get', 'status'], 'priority', 4, 7],
        'circle-stroke-color': '#14241b', 'circle-stroke-width': 1.5 } });
    map.addImage('wind-arrow', weatherArrow(), { pixelRatio: 2 });
    for (const id of ['weather-points', 'weather-arrows', 'operations-zone']) {
      map.addSource(id, { type: 'geojson', data: empty() });
    }
    map.addLayer({ id: 'operations-zone-fill', type: 'fill', source: 'operations-zone',
      filter: ['==', ['geometry-type'], 'Polygon'], paint: { 'fill-color': '#abefd4', 'fill-opacity': .10 } });
    map.addLayer({ id: 'operations-zone-outline', type: 'line', source: 'operations-zone',
      filter: ['!=', ['geometry-type'], 'Point'], paint: { 'line-color': '#abefd4', 'line-width': 2.3 } });
    map.addLayer({ id: 'operations-zone-points', type: 'circle', source: 'operations-zone',
      filter: ['==', ['geometry-type'], 'Point'], paint: { 'circle-radius': 4, 'circle-color': '#e3fff5',
        'circle-stroke-color': '#20392e', 'circle-stroke-width': 1.5 } });
    map.addLayer({ id: 'weather-points', type: 'circle', source: 'weather-points',
      paint: { 'circle-color': '#a7e9ef', 'circle-radius': 3.5, 'circle-stroke-width': 1.5, 'circle-stroke-color': '#152329' } });
    map.addLayer({ id: 'weather-arrows', type: 'symbol', source: 'weather-arrows',
      layout: { 'icon-image': 'wind-arrow', 'icon-rotate': ['get', 'toward'],
        'icon-size': ['interpolate', ['linear'], ['get', 'speed'], 0, .86, 15, 1.52],
        'icon-rotation-alignment': 'map', 'icon-pitch-alignment': 'map', 'icon-padding': 5 } });
    for (const [name, coordinates, kind, minZoom] of LABELS) {
      const label = document.createElement('div');
      label.textContent = name;
      label.className = `map-label ${kind}`;
      new maplibregl.Marker({ element: label, pitchAlignment: 'viewport' }).setLngLat(coordinates).addTo(map);
      function visibility() { label.hidden = map.getZoom() < minZoom; }
      map.on('zoom', visibility);
      visibility();
    }
    const refresh = installDataLoading(map, metadata);
    $('topo').addEventListener('change', () => {
      const visible = $('topo').checked;
      if (visible) map.getSource('audit').setData('/api/v1/city/height-audit');
      map.setLayoutProperty('topo-check', 'visibility', visible ? 'visible' : 'none');
    });
    for (const [box, layer] of Object.entries({ buildings: 'structures', roads: ['network', 'arterials', 'streets'],
      assets: 'facilities', slopes: 'grades', candidates: 'candidate-outline' })) {
      $(box).addEventListener('change', () => {
        for (const id of [layer].flat()) map.setLayoutProperty(id, 'visibility', $(box).checked ? 'visible' : 'none');
        refresh();
      });
    }
    $('terrain').addEventListener('change', () => {
      map.setTerrain($('terrain').checked ? { source: 'dem', exaggeration: 1 } : null);
      map.setLayoutProperty('hillshade', 'visibility', $('terrain').checked ? 'visible' : 'none');
    });
    installInteraction(map, metadata.building_evidence ?? {}, snowDetails);
    refresh().finally(resolve);
  }));
  map.on('error', event => {
    const message = event.error?.message;
    if (event.error?.name === 'AbortError' || message?.includes('signal is aborted')) return;
    if (message) $('scene-status').textContent = message.includes('data:image/')
      ? 'Карта · не удалось загрузить погодный слой' : `Карта · ${message.split('\n')[0].slice(0, 140)}`;
  });
  return {
    map, ready, locate,
    setRegion(region) {
      if (!region) {
        map.getSource('scenario-boundary').setData(empty());
        return;
      }
      const { west, south, east, north } = region;
      const ring = [[west, south], [east, south], [east, north], [west, north], [west, south]];
      map.getSource('scenario-boundary').setData({
        type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [ring] }
      });
    }
  };
}
