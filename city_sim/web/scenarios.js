import { $, format, request } from './api.js';
import { areas } from './map.js';

const form = $('scenario-form');
const kind = $('weather-kind');
const sampleSelect = $('weather-sample');
const storedSelect = $('scenarios');
const message = $('scenario-message');
const fieldRow = $('field-row');
const fieldType = $('field-type');
const fieldLegend = $('field-legend');

const localDate = value => new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Asia/Kamchatka', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'
}).format(new Date(value));
const regionFrom = box => Object.fromEntries(['west', 'south', 'east', 'north'].map((name, i) => [name, box[i]]));
const controls = ['speed', 'direction', 'height'].map(name => form.elements[name]);

export function createScenarios(view, health, revision) {
  let area = areas[0];
  let region = regionFrom(area.region);
  let current = null;
  let samples = new Map();
  let weatherToken = 0;
  let scenarioToken = 0;
  let field = null;
  let layer;
  let status = '';
  let tabIsScenario = false;

  function showStatus(value) { status = value; message.textContent = status; }
  function showSelection() {
    const pastVersion = current && current.data_revision !== revision;
    $('run').disabled = !current || !health.solver_connected || pastVersion;
    $('export').hidden = !current || pastVersion;
    if (pastVersion) {
      showStatus('Сценарий использует прежнюю геометрию. Для расчёта сохраните копию на текущей карте.');
    } else if (current) {
      const location = `/api/v1/scenarios/${current.scenario_id}/solver-input`;
      $('export').href = location;
      $('export').download = `wind-input-${current.scenario_id}.json`;
      if (!health.solver_connected) showStatus('Сценарий сохранён. Решатель ожидает подключения.');
    }
  }

  function setWind(wind) {
    form.elements.speed.value = wind.speed_mps;
    form.elements.direction.value = wind.direction_from_deg;
    form.elements.height.value = wind.reference_height_m;
  }

  function selectSample() {
    const sample = samples.get(sampleSelect.value);
    const detail = $('weather-detail');
    if (!sample) {
      detail.textContent = 'В архиве нет подходящей точки рядом с этой областью.';
      $('save').disabled = kind.value !== 'manual_scenario';
      return;
    }
    setWind(sample.wind);
    const extra = [sample.distance_m != null && `${sample.distance_m} м от центра области`,
      sample.temperature_c != null && `${format(sample.temperature_c)} °C`,
      sample.gust_mps != null && `порывы ${format(sample.gust_mps)} м/с`,
      sample.snow_mm_3h != null && `снег ${format(sample.snow_mm_3h)} мм/3 ч`,
      sample.snow_mm_1h != null && `снег ${format(sample.snow_mm_1h)} мм/ч`];
    detail.textContent = extra.filter(Boolean).join(' · ');
    $('save').disabled = false;
  }

  async function weatherOptions(savedWeather) {
    const token = ++weatherToken;
    const archive = kind.value !== 'manual_scenario';
    for (const control of controls) control.readOnly = archive;
    $('weather-row').hidden = !archive;
    $('weather-detail').hidden = !archive;
    $('save').disabled = archive;
    if (!archive) return;
    sampleSelect.replaceChildren(new Option('Загрузка…', ''));
    $('weather-detail').textContent = '';
    try {
      const [lon, lat] = area.center;
      const rows = await request(`/weather?kind=${kind.value}&lon=${lon}&lat=${lat}`);
      if (token !== weatherToken) return;
      if (savedWeather?.sample_id && savedWeather.valid_at && !rows.some(item => item.sample_id === savedWeather.sample_id)) {
        rows.unshift({ sample_id: savedWeather.sample_id, wind: current.wind, weather: savedWeather });
      }
      samples = new Map(rows.map(row => [row.sample_id, row]));
      sampleSelect.replaceChildren(...rows.map(row => new Option(
        `${localDate(row.weather.valid_at)} · ${format(row.wind.speed_mps)} м/с`, row.sample_id)));
      if (savedWeather) sampleSelect.value = savedWeather.sample_id;
      selectSample();
    } catch (error) {
      if (token === weatherToken) $('weather-detail').textContent = error.message;
    }
  }

  function updateRegion() {
    $('scenario-zone').textContent = `Расчётная область · ${area.label}`;
    view.setRegion(tabIsScenario ? region : null);
  }

  async function refreshSaved() {
    const records = await request('/scenarios');
    storedSelect.replaceChildren(new Option(records.length ? 'Открыть сценарий…' : 'Пока нет сценариев', ''),
      ...records.map(item => new Option(item.title, item.scenario_id)));
    storedSelect.value = current?.scenario_id ?? '';
  }

  function drawField() {
    if (!layer || !field) return;
    const range = layer.setData(field, fieldType.value);
    if (!range) {
      fieldLegend.textContent = 'В этом поле нет доступных значений.';
      return;
    }
    const units = fieldType.value === 'pressure' ? 'Па' : 'м/с';
    const pressure = field.pressure_reference === 'gauge' ? ' · избыточное' :
      field.pressure_reference === 'absolute' ? ' · абсолютное' : '';
    fieldLegend.textContent = `${field.model} · ${format(range.min)}…${format(range.max)} ${units}${fieldType.value === 'pressure' ? pressure : ''}`;
  }

  async function openField(runId, token) {
    const payload = await request(`/runs/${runId}/field`);
    const modes = [];
    if (payload.fields.pressure_pa) modes.push(new Option('Давление', 'pressure'));
    if (payload.fields.velocity_east_mps && payload.fields.velocity_north_mps) {
      modes.push(new Option('Скорость воздуха', 'wind'));
    }
    if (token !== scenarioToken || !modes.length) return;
    if (!layer) {
      const { FieldLayer } = await import('./field.js');
      if (token !== scenarioToken) return;
      layer = new FieldLayer();
      view.map.addLayer(layer);
      view.map.on('moveend', drawField);
      view.map.on('terrain', drawField);
      view.map.on('sourcedata', event => {
        if (event.sourceId === 'dem' && event.isSourceLoaded) drawField();
      });
    }
    field = payload;
    fieldType.replaceChildren(...modes);
    fieldRow.hidden = fieldLegend.hidden = false;
    drawField();
  }

  function clearField() {
    field = null;
    fieldRow.hidden = fieldLegend.hidden = true;
    layer?.setData(null);
  }

  $('scenario-form').addEventListener('submit', async event => {
    event.preventDefault();
    const title = form.elements.title.value.trim();
    if (!title) return showStatus('Введите название сценария.');
    const token = ++scenarioToken;
    const wind = { speed_mps: controls[0].valueAsNumber, direction_from_deg: controls[1].valueAsNumber,
      reference_height_m: controls[2].valueAsNumber };
    $('save').disabled = true;
    clearField();
    try {
      const data = await request('/scenarios', {
        title, region, wind, available_loose_snow: form.elements.snow.value,
        weather: kind.value === 'manual_scenario' ? { kind: kind.value } :
          { kind: kind.value, sample_id: sampleSelect.value }
      });
      if (token !== scenarioToken) return;
      current = data;
      showStatus('Сценарий сохранён.');
      showSelection();
      await refreshSaved();
    } catch (error) {
      if (token === scenarioToken) showStatus(error.message);
    } finally {
      if (token === scenarioToken) $('save').disabled = false;
    }
  });

  storedSelect.addEventListener('change', async () => {
    const token = ++scenarioToken;
    clearField();
    current = null;
    showStatus('');
    showSelection();
    if (!storedSelect.value) return;
    try {
      const record = await request(`/scenarios/${storedSelect.value}`);
      if (token !== scenarioToken) return;
      current = record;
      region = record.region;
      const key = ['west', 'south', 'east', 'north'].map(k => region[k]);
      area = areas.find(item => item.region.every((coordinate, index) => coordinate === key[index])) ||
        { label: 'Область сценария', center: [(key[0] + key[2]) / 2, (key[1] + key[3]) / 2] };
      view.locate(area, false);
      updateRegion();
      kind.value = record.weather.kind;
      form.elements.title.value = record.title;
      form.elements.snow.value = record.available_loose_snow;
      setWind(record.wind);
      showSelection();
      await weatherOptions(record.weather);
      const runs = await request(`/scenarios/${record.scenario_id}/runs`);
      const latest = runs[0];
      if (token === scenarioToken && latest) {
        showStatus(latest.error || 'Расчёт завершён.');
        if (latest.status === 'completed') await openField(latest.run_id, token);
      }
    } catch (error) {
      if (token === scenarioToken) showStatus(error.message);
    }
  });

  $('run').addEventListener('click', async () => {
    const token = ++scenarioToken;
    $('run').disabled = true;
    clearField();
    showStatus('Решатель обрабатывает сохранённый сценарий…');
    try {
      const run = await request(`/scenarios/${current.scenario_id}/runs`, {});
      if (token !== scenarioToken) return;
      showStatus(run.error || 'Расчёт завершён.');
      if (run.status === 'completed') await openField(run.run_id, token);
    } catch (error) {
      if (token === scenarioToken) showStatus(error.message);
    } finally {
      if (token === scenarioToken) showSelection();
    }
  });

  kind.addEventListener('change', () => weatherOptions());
  sampleSelect.addEventListener('change', selectSample);
  fieldType.addEventListener('change', drawField);
  const settings = $('wind-settings-dialog');
  const height = form.elements.height;
  height.addEventListener('invalid', () => {
    if (!settings.open) settings.showModal();
    height.focus();
  });
  settings.querySelector('.dialog-actions').addEventListener('submit', event => {
    if (!height.reportValidity()) event.preventDefault();
  });
  document.querySelectorAll('.tabs button').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach(tab => {
      const selected = tab === button;
      tab.setAttribute('aria-pressed', String(selected));
      $(tab.getAttribute('aria-controls')).hidden = !selected;
    });
    tabIsScenario = button.getAttribute('aria-controls') === 'scenario-panel';
    updateRegion();
  }));

  refreshSaved().catch(error => showStatus(error.message));
  updateRegion();
  return {
    setArea(next) {
      area = next;
      region = regionFrom(area.region);
      updateRegion();
      weatherOptions();
    }
  };
}
