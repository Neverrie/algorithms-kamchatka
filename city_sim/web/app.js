import { $, format, request } from './api.js';
import { createMap } from './map.js';
import { createOperations } from './operations.js';
import { createScenarios } from './scenarios.js';

for (const button of document.querySelectorAll('[data-dialog]')) {
  button.addEventListener('click', () => $(button.dataset.dialog).showModal());
}

async function start() {
  const [metadata, health] = await Promise.all([request('/city/metadata'), request('/health')]);
  $('solver-dot').classList.toggle('connected', health.snow_solver_connected || health.solver_connected);
  $('solver-status').textContent = health.snow_solver_connected ? 'Расчёт снега подключён' :
    health.solver_connected ? 'Решатель ветра настроен' : 'Расчёт снега не подключён';
  const evidence = metadata.building_evidence ?? {};
  $('candidate-row').hidden = !evidence.location_check?.candidate_outlines;
  $('gba-legend').hidden = !metadata.heights.estimated_from_gba;
  $('metadata').textContent = [
    `${format(metadata.building_count_total, 0)} зданий`, `${format(metadata.road_count_total, 0)} дуг в исходном графе`,
    `${format(metadata.heights.osm_height ?? 0, 0)} зданий с тегом высоты`,
    evidence.register_matched && `${format(evidence.register_matched, 0)} адресов сверены с реестром`,
    evidence.location_check?.image_matches && `${format(evidence.location_check.image_matches, 0)} контуров совпали со снимками`,
    metadata.terrain ? 'DSM 30 м · EGM2008' : 'DSM отсутствует'
  ].filter(Boolean).join(' · ');
  if (!window.maplibregl) throw new Error('Отсутствует MapLibre. Выполните python -m city_sim.vendor.');
  let scenarios;
  let operations;
  const view = createMap(metadata, area => {
    scenarios?.setArea(area);
    operations?.setArea(area);
  }, (where, nearby) => operations?.snowDetails(where, nearby) || []);
  await view.ready;
  scenarios = createScenarios(view, health, metadata.data_revision);
  operations = createOperations(view, health);
  document.documentElement.dataset.ready = 'true';
}

start().catch(error => {
  $('scene-status').textContent = error.message;
  $('solver-status').textContent = 'Ошибка запуска';
});
