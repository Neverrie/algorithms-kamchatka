import { $, empty, format, request } from './api.js';
import { createWeatherLayers, localTerm as time } from './weather_layers.js';
import { raster } from './raster_motion.js';
import { createSnowVolume } from './snow_volume.js';

const titles = {
  inspect: 'Район требует проверки', missing: 'Прогноз недоступен',
  forecast_below_threshold: 'Сигналов выше порога нет'
};
const reasons = { snow: 'снегопад', wind: 'сильный ветер или порывы', source_conflict: 'сведения расходятся' };
const datasets = { archive: 'forecast', 'atlas-history': 'history', live: 'live', historic: 'storm' };
const modelNames = {
  snow_cm: 'Свежий снег по модели', drift_cm: 'Наносы сверх снегопада',
  opposite_snow_cm: 'Снег при развороте ветра', combined: 'Приоритет проверки дорог',
  opposite: 'Приоритет при развороте ветра', terrain: 'Рельеф · индекс низины',
  uniform: 'Равномерный фон · индекс', weather_snow: 'Снегопад на входе модели',
  weather_wind: 'Скорость ветра на входе модели', weather_distance: 'До погодной точки'
};
const units = { snow_cm: 'см', drift_cm: 'см', opposite_snow_cm: 'см',
  weather_snow: 'см', weather_wind: 'м/с', weather_distance: 'км' };
const plural = new Intl.PluralRules('ru');

function point(coordinates, properties) {
  return { type: 'Feature', geometry: { type: 'Point', coordinates }, properties };
}

function lines(node, values) {
  node.replaceChildren(...values.map(value => {
    const p = document.createElement('p');
    p.textContent = value;
    return p;
  }));
}

function download(file, data) {
  const href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = href;
  link.download = file;
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

export function createOperations(view, health) {
  const map = view.map;
  const weather = createWeatherLayers(map);
  const volume = createSnowVolume(map);
  const scope = $('ops-scope');
  const source = $('ops-input');
  const term = $('ops-city-term');
  const resultStatus = $('ops-result-status');
  const layer = $('ops-layer');
  const slider = $('timeline-range');
  const play = $('timeline-play');
  const canvas = map.getCanvas();
  let district = 'center';
  let catalog;
  let archive;
  let network;
  let calculated;
  let busy = false;
  let snowRequest = 0;
  let weatherRequest = 0;
  let modelFrame;
  let player;
  let drawing = false;
  let vertices = [];
  let zone;
  let zoneMarker;
  const active = () => !$('operations-panel').hidden;
  const currentRows = () => archive?.rows.filter(r => r.valid_at === $('ops-term').value) ?? [];
  const atlasDataset = () => datasets[source.value];
  const stormId = () => $('ops-event').value;
  const weatherCatalog = dataset => dataset === 'storm' ? catalog?.storms?.[stormId()] : catalog?.[dataset];
  const zoneActions = document.createElement('div');
  zoneActions.className = 'zone-actions';
  zoneActions.setAttribute('role', 'group');
  zoneActions.setAttribute('aria-label', 'Действия над контуром');
  const redraw = document.createElement('button');
  const calculate = document.createElement('button');
  redraw.type = calculate.type = 'button';
  redraw.textContent = 'Начертить заново';
  calculate.className = 'primary';
  calculate.textContent = 'Рассчитать';
  zoneActions.append(redraw, calculate);
  zoneActions.addEventListener('click', event => event.stopPropagation());
  redraw.addEventListener('click', beginDraw);
  calculate.addEventListener('click', () => $('ops-form').requestSubmit());

  function positionZoneActions() {
    if (!zoneMarker || !zone?.length) return;
    const top = zone.reduce((first, vertex) => map.project(vertex).y < map.project(first).y ? vertex : first);
    zoneMarker.setLngLat(top);
  }
  map.on('move', positionZoneActions);

  function updateButton() {
    const needsTerm = Boolean(atlasDataset()) && source.value !== 'historic' && !term.value;
    const needsZone = scope.value === 'zone' && (!zone || drawing);
    const needsEvent = source.value === 'historic' && !$('ops-event').value;
    $('ops-calculate').disabled = !health.snow_solver_connected || needsTerm || needsZone || needsEvent || busy;
    calculate.disabled = $('ops-calculate').disabled;
    calculate.textContent = busy ? 'Считаю…' : 'Рассчитать';
  }

  function pause() {
    clearInterval(player);
    player = undefined;
    play.textContent = '▶';
    play.setAttribute('aria-label', 'Воспроизвести');
    play.setAttribute('aria-pressed', 'false');
  }

  function updateTimeline() {
    const resultFrames = calculated?.timeline?.length ? calculated.wind_frames : null;
    const options = resultFrames?.map(frame => frame.valid_at) || Array.from(term.options, option => option.value);
    const index = resultFrames ? modelFrame ?? resultFrames.length-1 : Math.max(term.selectedIndex, 0);
    $('weather-timeline').hidden = !options.length;
    if (!options.length) return;
    slider.max = options.length-1;
    slider.value = index;
    slider.disabled = options.length <= 1;
    $('timeline-title').textContent = resultFrames ? 'Накопление снега и ветер' : 'Шкала погоды';
    const label = i => options[i] ? time(options[i]) : `Шаг ${i+1}`;
    slider.setAttribute('aria-valuetext', label(index));
    $('timeline-current').textContent = label(index);
    $('timeline-start').textContent = label(0);
    $('timeline-end').textContent = label(options.length-1);
    $('ops-prev').disabled = term.selectedIndex <= 0;
    $('ops-next').disabled = term.selectedIndex < 0 || term.selectedIndex === term.options.length-1;
    play.disabled = options.length <= 1;
  }

  function showDistrictWeather() {
    const row = currentRows().find(item => item.district_id === district);
    const sample = row?.sample;
    $('ops-signal').dataset.state = row?.status || 'missing';
    $('ops-signal').textContent = row ? [titles[row.status], ...row.reasons.map(key => reasons[key]).filter(Boolean)].join(' · ')
      : 'Районный архив недоступен';
    lines($('ops-weather'), [
      sample?.snow_mm_3h == null ? 'Для точки нет числа снегопада' : `Снег за 3 ч · ${format(sample.snow_mm_3h)} мм водного эквивалента`,
      sample ? `Ветер · ${format(sample.wind.speed_mps)} м/с · от ${sample.wind.direction_from_deg}°` : '',
      network?.length_m_by_district[district] ? `Дороги около точки · ${format(network.length_m_by_district[district]/1000)} км графа` : ''
    ].filter(Boolean));
  }

  function paint() {
    const choice = layer.value;
    for (const button of document.querySelectorAll('[data-model]')) {
      button.setAttribute('aria-pressed', String(button.dataset.model === choice));
    }
    const temporal = calculated?.timeline?.length > 0 && modelFrame < calculated.timeline.length;
    const finalOnly = temporal && choice !== 'snow_cm' && calculated.overlays[choice];
    const image = finalOnly ? undefined : calculated?.overlays[choice];
    const visible = Boolean(image);
    const volumeVisible = visible && choice === 'snow_cm' && $('snow-volume').checked;
    $('weather-menu-model').hidden = !calculated;
    for (const button of document.querySelectorAll('[data-snow-view]')) {
      button.setAttribute('aria-pressed', String(choice === 'snow_cm' && (button.dataset.snowView === 'volume') === volumeVisible));
    }
    const rankKey = ['snow_cm', 'drift_cm'].includes(choice) ? 'combined' : choice === 'opposite_snow_cm' ? 'opposite' : choice;
    const rank = visible && !temporal ? calculated.ranking[rankKey] || [] : [];
    const markers = calculated?.weather_alerts?.map(alert => point(alert.coordinates, {
      status: alert.extrapolated ? 'extrapolated' : 'inspect'
    })) || [];
    const ranked = rank.filter(row => choice !== 'uniform').map(row => point([row.lon, row.lat], { status: 'priority' }));
    const flagged = new Set(currentRows().filter(r => r.status === 'inspect').map(r => r.district_id));
    const highlighted = active() && !calculated && scope.value === 'district' && source.value === 'archive'
      ? network?.features.filter(f => flagged.has(f.properties.district_id)) : [];
    map.getSource('operations-roads').setData({ ...empty(), features: highlighted || [] });
    map.getSource('operations-points').setData({ ...empty(), features: visible && choice.startsWith('weather_') ? markers : ranked });
    map.setLayoutProperty('operations-snow', 'visibility', visible && !volumeVisible ? 'visible' : 'none');
    volume.show(volumeVisible ? calculated.run_id : null, temporal ? modelFrame : undefined);
    $('snow-height-row').hidden = !volumeVisible;
    $('ops-model-ramp').hidden = volumeVisible;
    $('ops-scale-model').hidden = !visible;
    if (visible) {
      const frame = choice === 'snow_cm' && calculated.timeline?.[modelFrame];
      if (!volumeVisible) raster(map, 'operations-snow', calculated.bounds, frame || image);
      $('ops-scale-label').textContent = volumeVisible ? `Снег в 3D · высота ×${$('snow-height').value}` :
        frame ? 'Свежий снег к выбранному часу' : modelNames[choice];
      const unit = units[choice];
      const maximum = calculated.scales[choice] ?? 1;
      $('ops-scale-start').textContent = unit ? `0 ${unit}` : '0';
      $('ops-scale-mid').textContent = format(maximum/2);
      const capped = calculated.clipped?.includes(choice) ? '+' : '';
      $('ops-scale-end').textContent = unit ? `${maximum}${capped} ${unit}` : '1';
    }
    weather.legend();
    $('ops-roads-label').textContent = finalOnly
      ? 'Выбранный слой доступен на конец интервала.'
      : temporal
      ? 'Рейтинг дорог рассчитан на конец интервала.'
      : rank.length && choice !== 'uniform' ? 'Сегменты с наибольшим индексом на конец интервала.'
        : 'Для этого слоя список приоритетов не построен.';
    $('ops-roads').replaceChildren(...rank.filter(() => choice !== 'uniform').map(row => {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `${row.name || 'Сегмент дороги'} · ${format(row.value, 2)}`;
      button.title = row.edge_id;
      button.addEventListener('click', () => {
        $('results-dialog').close();
        map.easeTo({ center: [row.lon, row.lat], zoom: 16.2 });
      });
      li.append(button);
      if (row.fresh_snow_cm != null) {
        const label = document.createElement('div');
        label.textContent = `${format(row.fresh_snow_cm)} см снега` +
          (row.excess_cm == null ? '' : ` · ${row.excess_cm > 0 ? '+' : ''}${format(row.excess_cm)} см к фону`);
        li.append(label);
      }
      return li;
    }));
  }

  function resetSnow() {
    ++snowRequest;
    calculated = undefined;
    modelFrame = undefined;
    busy = false;
    $('ops-snow-result').hidden = true;
    $('ops-start-help').hidden = false;
    resultStatus.textContent = health.snow_solver_connected ? '' : 'Расчёт снега Данила не подключён.';
    weather.setResult(undefined);
    updateButton();
    updateTimeline();
    paint();
  }

  function selectWeather() {
    const dataset = atlasDataset();
    if (weatherCatalog(dataset)) weather.setWeather(dataset, term.value, dataset === 'storm' ? stormId() : undefined);
    else weather.setWeather(undefined, undefined);
    updateButton();
    updateTimeline();
  }

  function setTerms(preview = true) {
    const dataset = atlasDataset();
    const previously = term.value;
    const times = weatherCatalog(dataset)?.terms || [];
    term.replaceChildren(...times.map(value => new Option(time(value), value)));
    if (times.includes(previously)) term.value = previously;
    else if (dataset === 'storm' && times.length) term.value = weatherCatalog(dataset).peak_at;
    $('ops-storm-hours').textContent = dataset === 'storm' && times.length
      ? `${times.length} ${({ one: 'час', few: 'часа' })[plural.select(times.length)] || 'часов'}. Модель суммирует свежий снег за всю серию при условии, что он сохраняется на месте.` : '';
    if (dataset === 'live' && !times.includes(previously) && times.length > 1) {
      const now = Date.parse(catalog.live.retrieved_at);
      term.selectedIndex = Math.max(0, times.findIndex(t => Date.parse(t) >= now));
    }
    if (preview) selectWeather();
    else { updateButton(); updateTimeline(); }
  }

  async function updateLive(force = false) {
    const requestId = ++weatherRequest;
    $('ops-live-refresh').disabled = true;
    $('ops-live-state').textContent = 'Получаю прогноз…';
    try {
      const data = await request(force ? '/operations/atlas/live?refresh=true' : '/operations/atlas/live');
      if (requestId !== weatherRequest || source.value !== 'live') return;
      const changed = catalog.live?.sha256 && catalog.live.sha256 !== data.sha256;
      const resumeModel = changed && calculated?.input.source === 'live';
      catalog.live = data;
      weather.setCatalog(catalog);
      $('ops-live-state').textContent = `Open-Meteo · ${time(data.retrieved_at)}`;
      setTerms(!resumeModel);
      if (resumeModel && !$('ops-calculate').disabled) $('ops-form').requestSubmit();
    } catch (error) {
      if (requestId === weatherRequest) $('ops-live-state').textContent = error.message;
    } finally {
      if (requestId === weatherRequest) $('ops-live-refresh').disabled = false;
    }
  }

  function updateInput() {
    ++weatherRequest;
    pause();
    resetSnow();
    $('ops-draw-controls').hidden = scope.value !== 'zone';
    $('ops-manual').hidden = source.value !== 'manual';
    $('ops-archive-input').hidden = source.value !== 'archive';
    $('ops-city-term-label').hidden = !atlasDataset();
    $('ops-event-label').hidden = source.value !== 'historic';
    $('ops-storm-hours').hidden = source.value !== 'historic';
    $('ops-calculate').textContent = ({ city: 'Рассчитать снег для города',
      district: 'Рассчитать снег для района', zone: 'Рассчитать снег в контуре' })[scope.value];
    if (source.value === 'historic') $('ops-calculate').textContent = 'Рассчитать снег за всю серию';
    $('ops-live-controls').hidden = source.value !== 'live';
    $('ops-district-audit').hidden = scope.value !== 'district' || source.value !== 'archive';
    setTerms();
    if (source.value === 'live' && catalog) updateLive();
  }

  async function runCity(token, data) {
    const start = await request('/operations/city/runs', data);
    const url = `/operations/city/runs/${start.run_id}`;
    while (token === snowRequest) {
      const result = await request(url);
      if (token !== snowRequest) return;
      if (result.status === 'completed') return result;
      if (result.status === 'failed') throw new Error(result.error);
      resultStatus.textContent = result.total_tiles == null ? 'Готовлю сетку и входы…' :
        `Расчёт · ${result.completed_tiles} из ${result.total_tiles} участков`;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }

  function setFrame(index) {
    if (calculated?.timeline?.length) {
      modelFrame = index;
      weather.setFrame(index);
      if (atlasDataset()) {
        if (source.value === 'historic') term.value = calculated.wind_frames[index].valid_at;
        weather.setWeather(atlasDataset(), calculated.wind_frames[index].valid_at,
          source.value === 'historic' ? stormId() : undefined, calculated.source.atlas_sha256);
      }
      updateTimeline();
      paint();
      return;
    }
    if (index >= 0 && index < term.options.length && index !== term.selectedIndex) {
      term.selectedIndex = index;
      if (calculated || busy) resetSnow();
      selectWeather();
    }
  }

  slider.addEventListener('pointerdown', pause);
  slider.addEventListener('input', () => { pause(); setFrame(slider.valueAsNumber); });
  play.addEventListener('click', () => {
    if (player) { pause(); return; }
    if (slider.valueAsNumber >= Number(slider.max)) setFrame(0);
    play.textContent = 'Ⅱ';
    play.setAttribute('aria-label', 'Приостановить');
    play.setAttribute('aria-pressed', 'true');
    player = setInterval(() => {
      const next = slider.valueAsNumber+1;
      if (next > Number(slider.max)) pause();
      else setFrame(next);
    }, calculated?.timeline?.length ? 1200 : 900);
  });
  function changeTerm() {
    pause();
    if (source.value === 'historic' && calculated?.timeline?.length) {
      const stamp = Date.parse(term.value);
      const chosen = calculated.wind_frames.reduce((closest, frame, i, frames) =>
        Math.abs(Date.parse(frame.valid_at)-stamp) < Math.abs(Date.parse(frames[closest].valid_at)-stamp) ? i : closest, 0);
      setFrame(chosen);
      return;
    }
    if (source.value !== 'historic' || !busy) resetSnow();
    selectWeather();
  }
  term.addEventListener('change', changeTerm);
  for (const [id, step] of [['ops-prev', -1], ['ops-next', 1]]) $(id).addEventListener('click', () => {
    pause();
    const index = term.selectedIndex+step;
    if (index >= 0 && index < term.options.length) {
      term.selectedIndex = index;
      changeTerm();
    }
  });
  source.addEventListener('change', updateInput);
  $('ops-event').addEventListener('change', () => { pause(); resetSnow(); setTerms(); });
  $('ops-live-refresh').addEventListener('click', () => updateLive(true));
  setInterval(() => { if (source.value === 'live' && !busy && !player) updateLive(); }, 20*60*1000);
  for (const button of document.querySelectorAll('[data-model]')) button.addEventListener('click', () => {
    layer.value = button.dataset.model;
    layer.dispatchEvent(new Event('change'));
  });
  layer.addEventListener('change', paint);
  $('snow-volume').addEventListener('change', () => {
    if ($('snow-volume').checked) layer.value = 'snow_cm';
    paint();
  });
  for (const button of document.querySelectorAll('[data-snow-view]')) button.addEventListener('click', () => {
    layer.value = 'snow_cm';
    $('snow-volume').checked = button.dataset.snowView === 'volume';
    paint();
  });
  $('snow-height').addEventListener('change', paint);
  function showDraw() {
    const ring = vertices.length >= 3 ? [...vertices, vertices[0]] : null;
    const features = vertices.map(coordinates => point(coordinates, {}));
    if (ring) features.unshift({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [ring] }, properties: {} });
    else if (vertices.length === 2) features.unshift({ type: 'Feature', geometry: { type: 'LineString', coordinates: vertices }, properties: {} });
    map.getSource('operations-zone').setData({ ...empty(), features });
    canvas.dataset.drawing = String(drawing);
    if (drawing) map.doubleClickZoom.disable();
    else map.doubleClickZoom.enable();
    $('ops-draw-start').textContent = zone || drawing ? 'Начертить заново' : 'Начертить';
    $('ops-draw-undo').hidden = $('ops-draw-end').hidden = !drawing;
    $('ops-draw-undo').disabled = !vertices.length;
    $('ops-draw-end').disabled = vertices.length < 3;
    $('ops-draw-status').textContent = drawing ? `Ставьте точки на карте · ${vertices.length}/64 · Enter для завершения` :
      zone ? `Контур готов · ${zone.length} точек` : 'Задайте границу щелчками по карте.';
    if (zone && !drawing) {
      zoneMarker ??= new maplibregl.Marker({ element: zoneActions, anchor: 'bottom', offset: [0, -12] })
        .setLngLat(zone[0]).addTo(map);
      positionZoneActions();
    } else {
      zoneMarker?.remove();
      zoneMarker = undefined;
    }
    updateButton();
  }
  function finishDraw() {
    if (drawing && vertices.length >= 3) {
      drawing = false;
      zone = [...vertices];
      showDraw();
    }
  }
  function beginDraw() {
    zone = undefined;
    vertices = [];
    drawing = true;
    resetSnow();
    showDraw();
    canvas.focus({ preventScroll: true });
  }
  $('ops-draw-start').addEventListener('click', beginDraw);
  $('ops-draw-end').addEventListener('click', finishDraw);
  $('ops-draw-undo').addEventListener('click', () => { vertices.pop(); showDraw(); });
  map.on('click', event => {
    if (!drawing || vertices.length >= 64) return;
    const previous = vertices.at(-1);
    if (previous && Math.hypot(event.lngLat.lng-previous[0], event.lngLat.lat-previous[1]) < .000003) return;
    vertices.push([Number(event.lngLat.lng.toFixed(6)), Number(event.lngLat.lat.toFixed(6))]);
    showDraw();
  });
  scope.addEventListener('change', () => {
    if (scope.value === 'zone') {
      beginDraw();
    } else {
      drawing = false;
      vertices = [];
      zone = undefined;
    }
    showDraw();
    updateInput();
  });
  canvas.addEventListener('keydown', event => {
    if (!drawing) return;
    if (event.key === 'Enter') finishDraw();
    if (event.key === 'Backspace') {
      event.preventDefault();
      vertices.pop();
      showDraw();
    }
  });

  $('ops-snow-opacity').addEventListener('input', event =>
    map.setPaintProperty('operations-snow', 'raster-opacity', event.target.valueAsNumber/100));
  map.setPaintProperty('operations-snow', 'raster-opacity', $('ops-snow-opacity').valueAsNumber/100);
  $('ops-form').addEventListener('submit', async event => {
    event.preventDefault();
    if ($('ops-calculate').disabled) return;
    pause();
    const preserve = source.value === 'live' && calculated?.input.source === 'live';
    if (!preserve) resetSnow();
    const token = ++snowRequest;
    busy = true;
    updateButton();
    resultStatus.textContent = preserve ? 'Пересчитываю снег по новому прогнозу…' : 'Готовлю расчёт снега…';
    const payload = {
      source: { archive: 'openweather', 'atlas-history': 'atlas_history', historic: 'reanalysis',
        live: 'live', manual: 'manual' }[source.value]
    };
    if (scope.value === 'district') payload.district_id = district;
    if (scope.value === 'zone') payload.zone = zone;
    if (atlasDataset() && source.value !== 'historic') payload.valid_at = term.value;
    if (source.value === 'archive') payload.snow_to_liquid_ratio = $('ops-ratio').valueAsNumber;
    if (source.value === 'historic') payload.event_id = $('ops-event').value;
    if (source.value === 'live') {
      payload.weather_revision = catalog.live.sha256;
      payload.hours = Math.min(6, term.options.length-term.selectedIndex);
    }
    if (source.value === 'manual') Object.assign(payload, { wind_speed_ms: $('ops-speed').valueAsNumber,
      wind_from_deg: $('ops-direction').valueAsNumber, snowfall_cm_h: $('ops-snow').valueAsNumber });
    try {
      const result = await runCity(token, payload);
      if (!result || token !== snowRequest) return;
      const displayedTerm = preserve ? calculated.wind_frames[modelFrame]?.valid_at : undefined;
      calculated = result;
      const m = result.metrics;
      const matchingFrame = displayedTerm
        ? result.wind_frames.findIndex(frame => Date.parse(frame.valid_at) === Date.parse(displayedTerm))
        : -1;
      modelFrame = matchingFrame >= 0 ? matchingFrame : result.wind_frames.length-1;
      weather.setResult(result);
      weather.setFrame(modelFrame);
      if (result.input.source === 'live' || result.input.source === 'reanalysis') {
        if (source.value === 'historic') term.value = result.wind_frames[modelFrame].valid_at;
        weather.setWeather(atlasDataset(), result.wind_frames[modelFrame].valid_at,
          source.value === 'historic' ? stormId() : undefined, result.source.atlas_sha256);
      }
      layer.querySelector('[value="weather_distance"]').hidden = result.source.kind === 'manual';
      if (!preserve) layer.value = 'snow_cm';
      $('ops-snow-result').hidden = false;
      $('ops-start-help').hidden = true;
      const count = m.computed_tiles ?? 0;
      const label = ({ one: 'участок', few: 'участка' })[plural.select(count)] || 'участков';
      const dates = result.source.kind === 'reanalysis'
        ? ` · ${time(result.source.start_at)} — ${time(result.source.end_at)}` : '';
      $('ops-calc-summary').textContent = `${result.district} · ${count} ${label} · ${format(m.road_m_covered/1000)} км дорог${dates}`;
      resultStatus.textContent = '';
      lines($('ops-snow-checks'), [
        `Модель Данила · ${result.provenance.engine_commit.slice(0, 8)}`,
        `Погодных входов · ${m.distinct_weather_inputs}. Пропуски на ${m.missing_weather_tiles ?? 0} участках.`,
        `Сегментов с оценкой · ${m.road_segments_covered ?? 0}`,
        ...(m.road_m_weather_alert == null ? [] : [`Погодный сигнал · ${format(m.road_m_weather_alert/1000)} км`,
          `Источник дальше 3 км · ${m.extrapolated_weather_tiles ?? 0} участков`]),
        'Свежий снег оценён моделью. Состояние покрытия и старый снег требуют отдельной оценки.'
      ]);
      const [w, s, e, n] = result.fit_bounds;
      if (active() && !preserve) map.fitBounds([[w, s], [e, n]], { padding: 52, maxZoom: 17, duration: 550 });
      updateTimeline();
      paint();
    } catch (error) {
      if (token === snowRequest) resultStatus.textContent = error.message;
    } finally {
      if (token === snowRequest) { busy = false; updateButton(); }
    }
  });
  $('ops-snow-json').addEventListener('click', () => {
    if (!calculated) return;
    const link = document.createElement('a');
    link.href = `/api/v1/operations/city/runs/${calculated.run_id}/replay`;
    link.click();
  });
  $('ops-weather-json').addEventListener('click', () => { if (archive) download('district-weather.json', archive); });
  $('ops-period').addEventListener('change', loadDistrictArchive);
  $('ops-term').addEventListener('change', showDistrictWeather);

  async function loadDistrictArchive() {
    try {
      const result = await request(`/operations/weather?period=${$('ops-period').value}`);
      archive = result;
      const terms = [...new Set(archive.rows.map(row => row.valid_at))];
      $('ops-term').replaceChildren(...terms.map(value => new Option(time(value), value)));
      $('ops-weather-json').disabled = false;
      const m = archive.metrics;
      lines($('ops-weather-checks'), [
        `Архив · районных сигналов ${m.candidate_inspection_slots}, общий прогноз ${m.baseline_inspection_slots}`,
        `Покрытие · ${m.forecast_terms} из ${m.district_terms} записей по районам и срокам`
      ]);
      showDistrictWeather();
      paint();
    } catch (error) { $('ops-signal').textContent = error.message; }
  }
  request('/operations/atlas').then(data => {
    catalog = data;
    weather.setCatalog(catalog);
    if (source.value === 'archive' && !data.forecast.terms.length) {
      source.value = 'historic';
    }
    updateInput();
  }).catch(error => { resultStatus.textContent = error.message; });
  request('/operations/city/catalog').then(data => {
    $('ops-event').replaceChildren(...data.storms.map(event => new Option(event.title, event.event_id)));
    updateButton();
    if (source.value === 'historic') setTerms();
  }).catch(error => { resultStatus.textContent = error.message; });
  request('/operations/network').then(data => { network = data; showDistrictWeather(); }).catch(() => {});
  loadDistrictArchive();

  return {
    async snowDetails(where, nearby) {
      if (!calculated) return [];
      const { run_id, timeline } = calculated;
      const frame = modelFrame;
      const choice = layer.value;
      const query = new URLSearchParams({ lon: where.lng, lat: where.lat });
      if (nearby) query.set('nearby_m', 22);
      if (Number.isInteger(frame)) query.set('frame', frame);
      try {
        const row = await request(`/operations/city/runs/${run_id}/sample?${query}`);
        if (calculated?.run_id !== run_id || modelFrame !== frame) return [];
        if (row.snow_cm == null) return ['В этой точке нет ячейки расчёта снега.'];
        return [
          `Свежий снег по модели · ${format(choice === 'opposite_snow_cm' && frame >= timeline.length
            ? row.opposite_snow_cm ?? row.snow_cm : row.snow_cm)} см`,
          row.weather_snow_cm == null ? null : `Снегопад на входе · ${format(row.weather_snow_cm)} см`,
          row.weather_snow_cm == null ? null : `К фону · ${row.snow_cm > row.weather_snow_cm ? '+' : ''}${format(row.snow_cm-row.weather_snow_cm)} см`,
          row.sample_distance_m ? `Ближайшая открытая ячейка · ${row.sample_distance_m} м от точки` : null,
          `Ячейка модели · ${row.cell_width_m} × ${row.cell_height_m} м`
        ].filter(Boolean);
      } catch (error) { return [error.message]; }
    },
    setArea(area) {
      district = area.id;
      if (scope.value === 'district') resetSnow();
      showDistrictWeather();
    }
  };
}
