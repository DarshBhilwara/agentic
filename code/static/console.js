(() => {
  'use strict';
  const DEFAULTS = {
    program_enabled: true,
    semantic_enabled: true,
    kernel_enabled: true,
    device_discovery_enabled: false,
    inference_syscalls: true,
    host_metrics: true,
    traces_enabled: true,
    metrics_enabled: true,
    logs_enabled: true,
    content_capture: 'full',
    retention_days: 30,
    max_events_per_run: 10000,
    kernel_retention_days: 30,
    archive_mb: 64,
    archive_files: 5,
    discovery_seconds: 5,
    worker_replicas: 2,
    agent_root: '/home/agentic/agentic',
    inference_cache: '/home/iiitd/Documents/agentic/model-cache',
    model_name: 'qwen',
    vllm_url: 'http://vllm-service:8000/v1'
  };
  const STORAGE_KEY = 'telemetry-console:draft:v1';
  const DEMO = {
    devices: {
      source: 'demo',
      observed_at: 'Sample snapshot',
      nodes: [{
        name: 'research-node-01',
        ready: true,
        capacity: {
          cpu: '16',
          memory: '64Gi',
          'nvidia.com/gpu': '1'
        },
        allocatable: {
          cpu: '15',
          memory: '60Gi',
          'nvidia.com/gpu': '1'
        },
        version: '—',
        labels: {
          role: 'inference'
        }
      }],
      pods: [{
        name: 'telemetry-kernel-7gk2m',
        namespace: 'agentic',
        phase: 'Running',
        containers: [{
          name: 'collector',
          ready: true,
          restart_count: 0
        }],
        node: 'research-node-01'
      }, {
        name: 'agent-worker-6f8bd',
        namespace: 'agentic',
        phase: 'Running',
        containers: [{
          name: 'worker',
          ready: true,
          restart_count: 0
        }, {
          name: 'sidecar',
          ready: true,
          restart_count: 0
        }],
        node: 'research-node-01'
      }],
      collectors: [{
        name: 'kernel collector',
        status: 'Ready',
        coverage: 'Sample only'
      }],
      warnings: ['Illustrative sample data; no Kubernetes API was queried.']
    },
    runs: {
      source: 'demo',
      runs: [{
        id: 'sample-run-001',
        program_id: 'research-task',
        status: 'completed',
        started_at: '2026-10-17T10:24:00Z',
        completed_at: '2026-10-17T10:24:18Z',
        summary: 'Sample run completed with one tool call.'
      }, {
        id: 'sample-run-002',
        program_id: 'model-evaluation',
        status: 'running',
        started_at: '2026-10-17T11:02:00Z',
        summary: 'Sample execution in progress.'
      }]
    }
  };
  let draft = loadDraft(),
    token = null,
    mode = 'offline',
    devices = null,
    runs = [],
    runsSource = null,
    selectedRun = null,
    detail = null,
    detailTab = 'summary',
    selectedReports = new Set(),
    manifestFiles = [],
    epoch = 0,
    draftRevision = 0,
    controller = null,
    noticeTimer = null;
  const $ = id => document.getElementById(id),
    escText = node => node;

  function loadDraft() {
    try {
      const v = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'),
        merged = {
          ...DEFAULTS,
          ...v
        };
      for (const [k, x] of Object.entries(DEFAULTS))
        if (typeof merged[k] !== typeof x) merged[k] = x;
      for (const k of ['retention_days', 'max_events_per_run', 'kernel_retention_days', 'archive_mb', 'archive_files', 'discovery_seconds', 'worker_replicas'])
        if (!Number.isFinite(merged[k])) merged[k] = DEFAULTS[k];
      if (!['off', 'redacted', 'full'].includes(merged.content_capture)) merged.content_capture = DEFAULTS.content_capture;
      return merged
    } catch {
      return {
        ...DEFAULTS
      }
    }
  }

  function saveDraft() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
      flash('Draft saved in this browser. It has not been applied to Kubernetes.')
    } catch {
      flash('Could not save draft in browser storage. You can still download the settings.', 'warning')
    }
  }

  function flash(message, kind = 'success') {
    const n = $('notice');
    n.textContent = message;
    n.className = `notice ${kind==='error'?'error':kind==='warning'?'warning':''}`;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => n.classList.add('hidden'), 5500)
  }

  function setMode(next) {
    if (mode !== next) clearSensitiveUi();
    mode = next;
    const banner = $('mode-banner'),
      badge = $('connection-badge');
    banner.classList.remove('hidden', 'demo', 'offline');
    badge.className = 'badge';
    if (next === 'demo') {
      banner.classList.add('demo');
      banner.textContent = 'DEMO MODE · Sample devices and runs only. No cluster connection or collection is active.';
      badge.classList.add('badge-demo');
      $('connection-label').textContent = 'Demo data'
    } else if (next === 'live') {
      banner.textContent = 'Connected · Observations are fetched from the telemetry service.';
      badge.classList.add('badge-live');
      $('connection-label').textContent = 'Connected'
    } else {
      banner.classList.add('offline');
      banner.textContent = 'OFFLINE · Draft configuration is available. Connect to a telemetry gateway to view real devices and runs.';
      badge.classList.add('badge-muted');
      $('connection-label').textContent = 'Not connected'
    }
    $('refresh-button').disabled = next === 'offline';
    $('connect-button').textContent = next === 'live' ? 'Disconnect' : 'Connect';
    $('connect-button').classList.toggle('button-secondary', next === 'live');
    $('connect-button').classList.toggle('button-primary', next !== 'live')
  }

  function clearSensitiveUi() {
    devices = null;
    runs = [];
    runsSource = null;
    selectedRun = null;
    detail = null;
    selectedReports.clear();
    manifestFiles = [];
    $('preview-panel').classList.add('hidden');
    $('preview-files').replaceChildren();
    $('run-detail').replaceChildren(empty('Select a run', 'Choose an execution to inspect its retained telemetry.'));
    $('report-result').textContent = 'Select one or more runs to continue.';
    $('report-result').className = 'report-result';
    $('device-warning').replaceChildren();
  }

  function el(tag, attrs = {}, children = []) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k === 'htmlFor') n.htmlFor = v;
      else if (k === 'checked' || k === 'disabled' || k === 'selected') n[k] = Boolean(v);
      else if (k.startsWith('on')) n.addEventListener(k.slice(2).toLowerCase(), v);
      else n.setAttribute(k, v)
    }
    for (const c of Array.isArray(children) ? children : [children])
      if (c != null) n.append(typeof c === 'string' ? document.createTextNode(c) : c);
    return n
  }

  function fmt(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v)
  }

  function when(v) {
    if (!v) return '—';
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString()
  }

  function badge(text, kind = 'unknown') {
    return el('span', {
      class: `state-chip ${kind}`,
      text
    })
  }

  function statusKind(s) {
    const v = String(s || '').toLowerCase();
    if (['failed', 'error', 'crash', 'unavailable', 'not ready'].some(x => v.includes(x))) return 'offline';
    if (['ready', 'running', 'completed', 'success', 'succeeded', 'healthy'].some(x => v.includes(x))) return 'observed';
    return 'unknown'
  }
  const layerDefs = [
    ['program', '01', 'Program', 'Run/step measurements and tool timings.', 'program_enabled'],
    ['semantic', '02', 'Semantic', 'Intent, execution graphs and procedures.', 'semantic_enabled'],
    ['kernel', '03', 'Kernel', 'Syscalls correlated to process and active tool scope.', 'kernel_enabled']
  ];

  function renderLayers() {
    const root = $('layer-cards');
    root.replaceChildren();
    for (const [, num, name, desc, key] of layerDefs) {
      const label = el('label', {
          class: 'toggle'
        }),
        input = el('input', {
          type: 'checkbox',
          'aria-label': `${name} layer enabled`,
          'data-draft-key': key
        });
      input.checked = !!draft[key];
      input.addEventListener('change', () => updateDraft(key, input.checked));
      label.append(input, el('span', {
        class: 'toggle-track',
        'aria-hidden': 'true'
      }));
      const card = el('article', {
        class: 'card layer-card'
      });
      card.append(el('div', {
        class: 'layer-title-row'
      }, [el('span', {
        class: 'layer-index',
        text: `LAYER ${num}`
      }), label]), el('h3', {
        text: name
      }), el('p', {
        class: 'layer-description',
        text: desc
      }), el('hr', {
        class: 'layer-divider'
      }), el('div', {
        class: 'layer-meta'
      }, [el('span', {
        text: 'Observed state'
      }), badge(mode === 'live' ? 'Connected source' : mode === 'demo' ? 'Sample only' : 'Unknown', mode === 'live' ? 'observed' : mode === 'demo' ? 'offline' : 'unknown')]));
      root.append(card)
    }
  }

  function invalidatePreview() {
    draftRevision++;
    manifestFiles = [];
    $('preview-panel').classList.add('hidden');
    $('preview-files').replaceChildren()
  }

  function updateDraft(key, value) {
    draft[key] = value;
    invalidatePreview();
    document.querySelectorAll(`[data-draft-key="${key}"], #cfg-${key}`).forEach(input => {
      if (input.type === 'checkbox') input.checked = !!value;
      else input.value = value
    })
  }

  function go(page) {
    const valid = ['overview', 'collection', 'devices', 'runs', 'reports'].includes(page) ? page : 'overview';
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === `page-${valid}`));
    document.querySelectorAll('.nav-link').forEach(a => a.classList.toggle('active', a.dataset.page === valid));
    $('crumb-title').textContent = valid[0].toUpperCase() + valid.slice(1);
    document.querySelector('.sidebar').classList.remove('open');
    location.hash = valid;
    if (valid === 'devices') renderDevices();
    if (valid === 'runs') renderRuns();
    if (valid === 'reports') renderReportChoices()
  }
  document.addEventListener('click', e => {
    const g = e.target.closest('[data-goto]');
    if (g) {
      e.preventDefault();
      go(g.dataset.goto)
    }
    const nav = e.target.closest('[data-page]');
    if (nav) {
      e.preventDefault();
      go(nav.dataset.page)
    }
    if (e.target.closest('[data-refresh]')) refreshAll()
  });
  $('menu-toggle').addEventListener('click', () => $('main-nav').parentElement.classList.toggle('open'));

  function inputField(key, label, type = 'number', attrs = {}) {
    const id = `cfg-${key}`,
      wrap = el('div', {
        class: 'setting-item'
      }),
      txt = el('div', {
        class: 'setting-text'
      }, el('label', {
        htmlFor: id,
        text: label
      }));
    let input;
    if (type === 'switch') {
      input = el('input', {
        id,
        type: 'checkbox',
        'aria-label': label,
        'data-draft-key': key
      });
      input.checked = !!draft[key];
      input.addEventListener('change', () => updateDraft(key, input.checked));
      const toggle = el('label', {
        class: 'toggle',
        htmlFor: id
      }, [input, el('span', {
        class: 'toggle-track',
        'aria-hidden': 'true'
      })]);
      wrap.append(txt, toggle);
      return wrap
    }
    if (type === 'select') {
      input = el('select', {
        id,
        class: 'field-control',
        'data-draft-key': key
      });
      for (const opt of attrs.options) input.append(el('option', {
        value: opt,
        text: opt
      }));
      input.value = draft[key];
    } else {
      input = el('input', {
        id,
        type,
        class: `field-control${attrs.wide?' wide':''}`,
        'data-draft-key': key,
        ...(attrs.min !== undefined ? {
          min: String(attrs.min)
        } : {}),
        ...(attrs.max !== undefined ? {
          max: String(attrs.max)
        } : {}),
        ...(attrs.step ? {
          step: String(attrs.step)
        } : {})
      });
      input.value = draft[key]
    }
    input.addEventListener('input', () => updateDraft(key, type === 'number' ? (input.value === '' ? '' : Number(input.value)) : input.value));
    wrap.append(txt, input);
    return wrap
  }
  const configSections = [
    ['Layers', ['program_enabled', 'semantic_enabled', 'kernel_enabled'],
      ['Program measurements', 'Intent and graph events', 'Kernel syscall collection']
    ],
    ['Signals & scope', ['inference_syscalls', 'host_metrics', 'traces_enabled', 'metrics_enabled', 'logs_enabled'],
      ['Inference workload syscalls', 'Host metrics', 'Traces', 'Metrics', 'Logs']
    ],
    ['Capture & retention', ['content_capture', 'retention_days', 'max_events_per_run', 'kernel_retention_days', 'archive_mb', 'archive_files'],
      ['Content capture', 'Run retention (days)', 'Maximum events per run', 'Kernel retention (days)', 'Archive size (MB)', 'Archive files']
    ],
    ['Discovery & workloads', ['device_discovery_enabled', 'discovery_seconds', 'worker_replicas', 'agent_root', 'inference_cache', 'model_name', 'vllm_url'],
      ['Read-only device discovery', 'Discovery interval (seconds)', 'Worker replicas', 'Agent root path', 'Inference cache path', 'Model name', 'vLLM endpoint']
    ]
  ];

  function renderConfig() {
    const root = $('config-sections');
    root.replaceChildren();
    for (const [name, keys, labels] of configSections) {
      const section = el('section', {
        class: 'form-section'
      });
      section.append(el('h2', {
        text: name
      }), el('p', {
        class: 'form-section-intro',
        text: name === 'Layers' ? 'Enable or disable each instrumentation layer in the draft.' : name === 'Signals & scope' ? 'Select supporting signals and inference workload capture.' : name === 'Capture & retention' ? 'Bound captured content, archive size and record retention.' : 'Set workload paths and collector discovery parameters.'
      }));
      const grid = el('div', {
        class: 'setting-grid'
      });
      keys.forEach((k, i) => grid.append(inputField(k, labels[i], k.endsWith('_enabled') || ['inference_syscalls', 'host_metrics', 'traces_enabled', 'metrics_enabled', 'logs_enabled'].includes(k) ? 'switch' : k === 'content_capture' ? 'select' : k === 'agent_root' || k === 'inference_cache' || k === 'model_name' || k === 'vllm_url' ? 'text' : 'number', k === 'content_capture' ? {
        options: ['off', 'redacted', 'full']
      } : ['agent_root', 'inference_cache', 'vllm_url'].includes(k) ? {
        wide: true
      } : {})));
      section.append(grid);
      root.append(section)
    }
  }

  function collectForm() {
    for (const k of Object.keys(draft)) {
      const x = $(`cfg-${k}`);
      if (x) draft[k] = x.type === 'checkbox' ? x.checked : x.type === 'number' ? (x.value === '' ? '' : Number(x.value)) : x.value
    }
  }

  function validateDraft() {
    collectForm();
    const errors = [];
    for (const k of ['retention_days', 'max_events_per_run', 'kernel_retention_days', 'archive_mb', 'archive_files', 'discovery_seconds', 'worker_replicas']) {
      const n = Number(draft[k]);
      if (!Number.isInteger(n) || n < 1) errors.push(`${k.replaceAll('_',' ')} must be a positive whole number.`)
    }
    for (const k of ['agent_root', 'inference_cache', 'model_name', 'vllm_url'])
      if (!String(draft[k]).trim()) errors.push(`${k.replaceAll('_',' ')} is required.`);
    try {
      const u = new URL(draft.vllm_url);
      if (!['http:', 'https:'].includes(u.protocol)) throw Error()
    } catch {
      errors.push('vLLM endpoint must be a valid http or https URL.')
    }
    return errors
  }
  $('config-form').addEventListener('submit', async e => {
    e.preventDefault();
    const errors = validateDraft();
    if (errors.length) {
      flash(errors.join(' '), 'error');
      return
    }
    if (mode !== 'live') {
      flash('Connect to a gateway to validate a preview. Draft validation passed locally; no manifests were generated.', 'warning');
      return
    }
    const b = $('preview-config'),
      revision = draftRevision,
      requestEpoch = epoch;
    b.disabled = true;
    b.textContent = 'Validating…';
    try {
      const data = await jsonApi('/console/config/preview', {
        method: 'POST',
        body: JSON.stringify(draft)
      });
      if (revision !== draftRevision || requestEpoch !== epoch || mode !== 'live') return;
      manifestFiles = Array.isArray(data.files) ? data.files : [];
      renderPreview(data)
    } catch (err) {
      if (requestEpoch === epoch && err.name !== 'AbortError') flash(`Preview failed: ${err.message}`, 'error')
    } finally {
      b.disabled = false;
      b.textContent = 'Validate & preview'
    }
  });

  function renderPreview(data) {
    $('preview-panel').classList.remove('hidden');
    $('preview-status').textContent = `${manifestFiles.length} file${manifestFiles.length===1?'':'s'} · rollout required`;
    const root = $('preview-files');
    root.replaceChildren();
    for (const f of manifestFiles) {
      const d = el('details', {
        class: 'preview-file'
      });
      d.append(el('summary', {
        text: fmt(f.name)
      }), el('pre', {
        text: fmt(f.content)
      }));
      root.append(d)
    }
    if (Array.isArray(data.warnings) && data.warnings.length) flash(data.warnings.map(fmt).join(' '), 'warning');
    else flash('Server validated the preview. Review generated files; rollout is still required.')
  }
  $('save-config').addEventListener('click', () => {
    collectForm();
    invalidatePreview();
    saveDraft()
  });
  $('reset-config').addEventListener('click', () => {
    draft = {
      ...DEFAULTS
    };
    invalidatePreview();
    renderConfig();
    renderLayers();
    saveDraft()
  });
  $('download-config').addEventListener('click', () => {
    collectForm();
    download('telemetry-config-draft.json', JSON.stringify(draft, null, 2), 'application/json')
  });
  $('download-manifests').addEventListener('click', () => download('telemetry-manifests.yaml', manifestFiles.filter(f => f.name !== 'settings.json').map(f => `# ${fmt(f.name)}\n${fmt(f.content)}`).join('\n---\n'), 'text/yaml'));

  function download(name, data, mime) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([data], {
      type: mime
    }));
    a.download = name;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }
  async function api(path, opts = {}) {
    const headers = {
      'Accept': 'application/json',
      ...(opts.body ? {
        'Content-Type': 'application/json'
      } : {}),
      ...(token ? {
        'X-API-Key': token
      } : {})
    };
    const response = await fetch(path, {
      ...opts,
      headers,
      signal: controller?.signal
    });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText||'request failed'}`);
    return response
  }
  async function jsonApi(path, opts) {
    const r = await api(path, opts);
    return r.json()
  }
  $('connect-button').addEventListener('click', () => mode === 'live' ? disconnect() : openConnect());

  function openConnect() {
    $('connection-error').classList.add('hidden');
    $('connection-dialog').showModal();
    $('api-token').focus()
  }
  $('demo-button').addEventListener('click', () => {
    invalidate();
    token = null;
    setMode('demo');
    devices = DEMO.devices;
    runs = DEMO.runs.runs.map(x => ({
      ...x
    }));
    runsSource = 'demo';
    $('connection-dialog').close();
    $('api-token').value = '';
    renderAll()
  });
  $('connection-form').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = $('connection-submit'),
      candidate = $('api-token').value.trim();
    if (!candidate) return;
    btn.disabled = true;
    btn.textContent = 'Connecting…';
    $('connection-error').classList.add('hidden');
    invalidate();
    controller = new AbortController();
    const requestEpoch = epoch;
    token = candidate;
    try {
      const [config, d, r] = await Promise.all([jsonApi('/console/config'), jsonApi('/console/devices'), jsonApi('/console/runs?limit=100')]);
      if (requestEpoch !== epoch) return;
      if (!config?.config || !d || !Array.isArray(r?.runs)) throw new Error('Gateway returned an unexpected response.');
      setMode('live');
      draft = {
        ...DEFAULTS,
        ...config.config,
        ...draft
      };
      devices = d;
      runs = r.runs;
      runsSource = r.source;
      $('connection-dialog').close();
      $('api-token').value = '';
      renderConfig();
      renderAll();
      flash(`Connected. Configuration source: ${fmt(config.source)}.`)
    } catch (err) {
      if (requestEpoch !== epoch) return;
      token = null;
      invalidate();
      $('connection-error').textContent = err.name === 'AbortError' ? 'Connection cancelled.' : `Could not connect: ${err.message}`;
      $('connection-error').classList.remove('hidden')
    } finally {
      btn.disabled = false;
      btn.textContent = 'Connect'
    }
  });

  function invalidate() {
    epoch++;
    if (controller) controller.abort();
    controller = new AbortController()
  }

  function disconnect() {
    invalidate();
    token = null;
    setMode('offline');
    renderAll();
    flash('Disconnected. Observation data and session token were cleared.')
  }
  async function refreshAll() {
    if (mode === 'demo') {
      devices = DEMO.devices;
      runs = DEMO.runs.runs.map(x => ({
        ...x
      }));
      runsSource = 'demo';
      renderAll();
      flash('Sample observations refreshed.', 'warning');
      return
    }
    if (mode !== 'live') return;
    invalidate();
    const myEpoch = epoch;
    try {
      const [d, r] = await Promise.all([jsonApi('/console/devices'), jsonApi('/console/runs?limit=100')]);
      if (myEpoch !== epoch || mode !== 'live') return;
      devices = d;
      runs = r.runs || [];
      runsSource = r.source;
      renderAll();
      if (selectedRun) await loadRun(selectedRun)
    } catch (err) {
      if (err.name !== 'AbortError' && myEpoch === epoch) flash(`Refresh failed: ${err.message}`, 'error')
    }
  }

  function renderAll() {
    renderLayers();
    renderOverview();
    renderDevices();
    renderRuns();
    renderReportChoices()
  }

  function renderOverview() {
    const deviceUnavailable = devices?.source === 'unavailable',
      runUnavailable = runsSource === 'unavailable',
      count = devices && Array.isArray(devices.nodes) && !deviceUnavailable ? devices.nodes.length : null;
    $('device-count').textContent = count === null ? '—' : String(count);
    $('device-status').textContent = deviceUnavailable ? 'Device state unavailable' : devices ? `${count} node${count===1?'':'s'} in ${mode==='demo'?'sample':'latest'} inventory` : 'No cluster observations';
    $('run-count').textContent = mode === 'offline' || runUnavailable ? '—' : String(runs.length);
    $('run-status').textContent = mode === 'offline' ? 'No run observations' : runUnavailable ? 'Run state unavailable' : `${mode==='demo'?'Sample':'Observed'} executions`;
    const root = $('overview-runs');
    root.replaceChildren();
    if (mode === 'offline' || runUnavailable || !runs.length) {
      root.append(empty(runUnavailable ? 'Run ledger unavailable' : 'No runs to show', mode === 'offline' ? 'Connect to a gateway or explicitly explore demo mode to view execution records.' : runUnavailable ? 'The telemetry service could not provide run observations.' : 'The connected ledger has no retained runs.'));
      return
    }
    root.append(runTable(runs.slice(0, 5), false))
  }

  function empty(title, desc) {
    return el('div', {
      class: 'empty-state'
    }, [el('span', {
      class: 'empty-icon',
      text: '·'
    }), el('h3', {
      text: title
    }), el('p', {
      text: desc
    })])
  }

  function table(headers, rows) {
    const t = el('table', {
        class: 'data-table'
      }),
      thead = el('thead'),
      tr = el('tr');
    headers.forEach(h => tr.append(el('th', {
      text: h
    })));
    thead.append(tr);
    const body = el('tbody');
    rows.forEach(row => body.append(row));
    t.append(thead, body);
    return t
  }

  function runTable(list, selectable) {
    const rows = list.map(r => {
      const tr = el('tr'),
        id = r.id ?? r.run_id ?? 'unknown';
      tr.append(el('td', {
        class: 'primary-cell',
        text: fmt(id)
      }), el('td', {
        text: fmt((r.program_id ?? r['task.id']))
      }), el('td', {}, badge(fmt(r.status), statusKind(r.status))), el('td', {
        text: when(r.started_at)
      }));
      if (selectable) {
        const td = el('td', {
            class: 'check-wrap'
          }),
          i = el('input', {
            type: 'checkbox',
            'aria-label': `Select run ${id}`
          });
        i.checked = selectedReports.has(String(id));
        i.addEventListener('change', () => toggleReport(String(id), i.checked));
        td.append(i);
        tr.append(td)
      }
      return tr
    });
    return table(selectable ? ['Run', 'Program', 'Status', 'Started', 'Report'] : ['Run', 'Program', 'Status', 'Started'], rows)
  }

  function recordsList(arr) {
    if (!Array.isArray(arr) || !arr.length) return null;
    return arr
  }

  function renderDevices() {
    const d = devices;
    const nodes = $('nodes-table'),
      pods = $('pods-table');
    nodes.replaceChildren();
    pods.replaceChildren();
    if (!d) {
      nodes.append(empty('No device observations', 'Connect to a gateway to discover Kubernetes nodes and their reported state.'));
      pods.append(empty('No pod observations', 'Collector and workload states appear here after a successful connection.'));
      $('collectors-json').textContent = 'No collector observations.';
      $('nodes-caption').textContent = 'State unknown';
      $('device-warning').replaceChildren();
      return
    }
    const unavailable = d.source === 'unavailable',
      ns = recordsList(d.nodes) || [],
      ps = recordsList(d.pods) || [];
    $('nodes-caption').textContent = unavailable ? 'State unavailable' : `${ns.length} observed · snapshot ${fmt(d.observed_at)}`;
    if (ns.length) nodes.append(table(['Node', 'State', 'CPU', 'Memory', 'GPU', 'Details'], ns.map(n => {
      const tr = el('tr'),
        name = n.name ?? n.node ?? n.metadata?.name,
        conditions = n.conditions,
        readyCondition = Array.isArray(conditions) ? conditions.find(c => c.type === 'Ready')?.status : undefined,
        rawReady = n.ready,
        st = rawReady === true ? 'Ready' : rawReady === false ? 'Not ready' : n.status ?? n.state ?? (readyCondition ? `Ready: ${readyCondition}` : undefined),
        cap = n.capacity || {},
        td = el('td');
      const det = el('details', {
        class: 'device-detail'
      });
      det.append(el('summary', {
        text: 'Inspect'
      }), el('pre', {
        text: JSON.stringify(n, null, 2)
      }));
      td.append(det);
      tr.append(el('td', {
        class: 'primary-cell',
        text: fmt(name)
      }), el('td', {}, badge(fmt(st), statusKind(st))), el('td', {
        text: fmt(cap.cpu ?? n.allocatable?.cpu)
      }), el('td', {
        text: fmt(cap.memory ?? n.allocatable?.memory)
      }), el('td', {
        text: fmt(cap['nvidia.com/gpu'] ?? n.allocatable?.['nvidia.com/gpu'])
      }), td);
      return tr
    })));
    else nodes.append(empty(unavailable ? 'Node state unavailable' : 'No nodes reported', unavailable ? 'The telemetry service could not inspect Kubernetes nodes.' : 'The connected API returned no node records.'));
    if (ps.length) pods.append(table(['Workload', 'Namespace', 'State', 'Ready containers', 'Restarts', 'Node'], ps.map(p => {
      const tr = el('tr'),
        status = p.phase ?? p.status,
        containers = Array.isArray(p.containers) ? p.containers : [],
        ready = containers.length ? `${containers.filter(c=>c.ready).length}/${containers.length}` : '—',
        restarts = containers.length ? containers.reduce((a, c) => a + (Number(c.restart_count) || 0), 0) : undefined;
      tr.append(el('td', {
        class: 'primary-cell',
        text: fmt(p.name ?? p.metadata?.name)
      }), el('td', {
        text: fmt(p.namespace ?? p.metadata?.namespace)
      }), el('td', {}, badge(fmt(status), statusKind(status))), el('td', {
        text: fmt(ready)
      }), el('td', {
        text: fmt(restarts)
      }), el('td', {
        text: fmt(p.node ?? p.node_name)
      }));
      return tr
    })));
    else pods.append(empty(unavailable ? 'Workload state unavailable' : 'No workloads reported', unavailable ? 'The telemetry service could not inspect Kubernetes pods.' : 'The connected API returned no pod records.'));
    $('collectors-json').textContent = JSON.stringify(d.collectors ?? [], null, 2);
    if (Array.isArray(d.warnings) && d.warnings.length) $('device-warning').replaceChildren(el('div', {
      class: 'notice warning',
      text: d.warnings.map(fmt).join(' · ')
    }));
    else $('device-warning').replaceChildren()
  }

  function matchRuns() {
    const q = $('run-search').value.trim().toLowerCase();
    return runs.filter(r => !q || [r.id, r.run_id, (r.program_id ?? r['task.id']), r.status, r.summary].some(x => String(x || '').toLowerCase().includes(q)))
  }
  $('run-search').addEventListener('input', () => {
    renderRuns();
    renderReportChoices()
  });

  function renderRuns() {
    const root = $('runs-list');
    root.replaceChildren();
    const list = matchRuns();
    $('run-list-count').textContent = mode === 'offline' || runsSource === 'unavailable' ? '—' : `${list.length} shown`;
    if (mode === 'offline') {
      root.append(empty('Connect to inspect runs', 'Use the connection button or enter demo mode to explore sample executions.'));
      return
    }
    if (runsSource === 'unavailable') {
      root.append(empty('Run ledger unavailable', 'The telemetry service could not provide run observations.'));
      return
    }
    if (!list.length) {
      root.append(empty('No matching runs', 'Try another search or refresh the run ledger.'));
      return
    }
    for (const r of list) {
      const id = String(r.id ?? r.run_id ?? 'unknown'),
        button = el('button', {
          class: `run-row${id===selectedRun?' selected':''}`,
          type: 'button',
          'aria-pressed': String(id === selectedRun)
        }),
        checkbox = el('input', {
          type: 'checkbox',
          'aria-label': `Select ${id} for report`,
          checked: selectedReports.has(id)
        });
      checkbox.addEventListener('click', e => e.stopPropagation());
      checkbox.addEventListener('change', e => toggleReport(id, e.target.checked));
      button.append(el('span', {
        class: 'check-wrap'
      }, checkbox), el('span', {
        class: 'run-row-main'
      }, [el('span', {
        class: 'run-id',
        text: id
      }), el('span', {
        class: 'run-sub',
        text: `${fmt((r.program_id ?? r['task.id']))} · ${when(r.started_at)}`
      })]), badge(fmt(r.status), statusKind(r.status)));
      button.addEventListener('click', () => loadRun(id));
      root.append(button)
    }
  }
  async function loadRun(id) {
    selectedRun = id;
    detailTab = 'summary';
    renderRuns();
    if (mode === 'demo') {
      detail = sampleDetail(id);
      renderDetail();
      return
    }
    if (mode !== 'live') return;
    invalidate();
    const my = epoch;
    detail = null;
    renderDetail();
    const loading = empty('Loading run details', 'Fetching retained run data from the telemetry service.');
    $('run-detail').replaceChildren(loading);
    try {
      const d = await jsonApi(`/console/runs/${encodeURIComponent(id)}`);
      if (my !== epoch || selectedRun !== id) return;
      detail = d;
      renderDetail()
    } catch (err) {
      if (err.name !== 'AbortError') $('run-detail').replaceChildren(empty('Could not load this run', err.message))
    }
  }

  function sampleDetail(id) {
    return {
      run: runs.find(r => String(r.id ?? r.run_id) === id) || {},
      summary: 'Sample run summary: the execution completed and invoked a file inspection tool.',
      steps: [{
        index: 0,
        step_status: 'completed',
        tools: [{
          tool_name: 'read_file',
          tool_call_id: 'sample-call-01'
        }],
        summary: 'Read a project file'
      }],
      events: [{
        type: 'tool_call',
        attributes: {
          tool_name: 'read_file',
          tool_call_id: 'sample-call-01',
          step_index: 0
        }
      }],
      kernel: {
        status: 'sample',
        records: [{
          timestamp_ns: 1729000000000,
          pid: 2184,
          tid: 2184,
          syscall_id: 0,
          syscall_name: 'read',
          tool_name: 'read_file',
          tool_call_id: 'sample-call-01',
          args: [3, 4096, 0, 0, 0, 0],
          ret: 4096,
          duration_ns: 81000
        }],
        total: 1,
        retained: 1,
        truncated: false,
        node_health: {
          status: 'sample',
          message: 'Sample only'
        }
      },
      procedural_graph: {
        nodes: 1,
        edges: 0
      },
      snapshot: {
        source: 'demo'
      }
    }
  }

  function renderDetail() {
    const root = $('run-detail');
    if (!detail) {
      root.replaceChildren(empty('Loading run details', 'Fetching the retained execution record.'));
      return
    }
    root.replaceChildren();
    const run = detail.run || detail,
      id = String(run.id ?? run.run_id ?? selectedRun);
    const h = el('div', {
      class: 'run-detail-header'
    }, [el('div', {}, [el('h2', {
      text: id
    }), el('p', {
      text: `${fmt((run.program_id ?? run['task.id']))} · Started ${when(run.started_at)}`
    })]), badge(fmt(run.status), statusKind(run.status))]);
    root.append(h);
    const tabs = el('div', {
      class: 'run-tabs',
      'role': 'tablist',
      'aria-label': 'Run detail sections'
    });
    for (const [key, title] of [
        ['summary', 'Summary'],
        ['steps', 'Steps'],
        ['semantic', 'Semantic'],
        ['kernel', 'Kernel']
      ]) {
      const b = el('button', {
        type: 'button',
        role: 'tab',
        'aria-selected': String(detailTab === key),
        class: detailTab === key ? 'active' : '',
        text: title
      });
      b.addEventListener('click', () => {
        detailTab = key;
        renderDetail()
      });
      tabs.append(b)
    }
    root.append(tabs);
    const body = el('div');
    root.append(body);
    if (detailTab === 'summary') {
      const k = el('div', {
        class: 'detail-kpis'
      }, [kpi('Steps', Array.isArray(detail.steps) ? detail.steps.length : '—'), kpi('Events', Array.isArray(detail.events) ? detail.events.length : '—'), kpi('Syscalls', detail.kernel?.total ?? '—')]);
      body.append(k, el('h3', {
        class: 'small',
        text: 'Run summary'
      }), el('p', {
        class: 'detail-text',
        text: fmt(detail.summary ?? run.summary)
      }), el('p', {
        class: 'muted small',
        text: `Snapshot: ${fmt(detail.snapshot?.source??(mode==='demo'?'sample':'telemetry service'))}`
      }));
      if (detail.procedural_graph) body.append(el('details', {
        class: 'raw-details'
      }, [el('summary', {
        text: 'Procedural graph'
      }), el('pre', {
        text: JSON.stringify(detail.procedural_graph, null, 2)
      })]))
    } else if (detailTab === 'steps') renderObjectTable(body, detail.steps, ['index', 'step_index', 'step_status', 'status', 'tool_name', 'tool_call_id', 'summary']);
    else if (detailTab === 'semantic') renderObjectTable(body, detail.events, ['timestamp', 'type', 'step_index', 'tool_name', 'tool_call_id', 'event']);
    else renderKernel(body, detail.kernel)
  }

  function kpi(label, value) {
    return el('div', {
      class: 'detail-kpi'
    }, [el('span', {
      text: label
    }), el('strong', {
      text: fmt(value)
    })])
  }

  function renderObjectTable(root, data, keys) {
    if (!Array.isArray(data) || !data.length) {
      root.append(empty('No records retained', 'This run has no retained records in this section.'));
      return
    }
    root.append(el('div', {
      class: 'table-wrap'
    }, table(keys.map(k => k.replaceAll('_', ' ')), data.map(o => {
      const tr = el('tr'),
        attrs = o.attributes && typeof o.attributes === 'object' ? o.attributes : {};
      keys.forEach(k => {
        let value = o[k] ?? attrs[k];
        if (k === 'tool_name' && value == null && Array.isArray(o.tools)) value = o.tools.map(t => t.tool_name ?? t.name ?? t).join(', ');
        if (k === 'tool_call_id' && value == null && Array.isArray(o.tools)) value = o.tools.map(t => t.tool_call_id ?? t.id).filter(Boolean).join(', ');
        if (k === 'event') value = o.attributes ?? o.event;
        tr.append(el('td', {
          text: fmt(value)
        }))
      });
      return tr
    }))))
  }

  function renderKernel(root, k) {
    if (!k) {
      root.append(empty('Kernel data unavailable', 'No kernel capture payload was returned for this run.'));
      return
    }
    const rawStatus = k.status,
      status = rawStatus && typeof rawStatus === 'object' ? rawStatus.state ?? rawStatus.status ?? JSON.stringify(rawStatus) : rawStatus ?? 'unknown',
      health = k.node_health;
    root.append(el('div', {
      class: 'detail-kpis'
    }, [kpi('Capture status', status), kpi('Total events', k.total ?? '—'), kpi('Retained', k.retained ?? '—'), kpi('Truncated', k.truncated === true ? 'Yes' : k.truncated === false ? 'No' : 'Unknown')]));
    if (health != null) root.append(el('details', {
      class: 'raw-details'
    }, [el('summary', {
      text: 'Node health'
    }), el('pre', {
      text: JSON.stringify(health, null, 2)
    })]));
    root.append(el('p', {
      class: 'muted small',
      text: 'Tool labels identify the active execution scope. They do not establish that a process syscall was causally triggered by that tool.'
    }));
    const rows = Array.isArray(k.records) ? k.records : [];
    if (rows.length) {
      const fields = ['pid', 'tid', 'syscall_id', 'syscall_name', 'tool_call_id', 'tool_name', 'args', 'ret', 'duration_ns'];
      root.append(el('div', {
        class: 'table-wrap code-scroll'
      }, table(fields, rows.map(r => {
        const tr = el('tr');
        fields.forEach(f => tr.append(el('td', {
          class: f === 'args' ? 'args-cell' : '',
          text: fmt(r[f] ?? (f === 'syscall_id' ? r.id : undefined))
        })));
        return tr
      }))))
    } else root.append(empty('No syscalls retained', 'The response contains no syscall records.'))
  }

  function toggleReport(id, on) {
    if (on && selectedReports.size >= 20 && !selectedReports.has(id)) {
      flash('Reports are limited to 20 selected runs.', 'warning');
      renderReportChoices();
      return
    }
    if (on) selectedReports.add(id);
    else selectedReports.delete(id);
    renderReportChoices()
  }

  function renderReportChoices() {
    const root = $('report-run-list');
    root.replaceChildren();
    $('report-count').textContent = `${selectedReports.size} selected`;
    if (mode === 'offline' || runsSource === 'unavailable') {
      root.append(empty(runsSource === 'unavailable' ? 'Run ledger unavailable' : 'No observed runs', mode === 'offline' ? 'Connect or explore sample data to choose runs.' : 'The telemetry service could not provide runs.'));
      $('generate-report').disabled = true;
      return
    }
    for (const r of matchRuns()) {
      const id = String(r.id ?? r.run_id ?? 'unknown'),
        input = el('input', {
          type: 'checkbox',
          'aria-label': `Include run ${id}`,
          checked: selectedReports.has(id)
        });
      input.addEventListener('change', () => toggleReport(id, input.checked));
      root.append(el('label', {
        class: 'report-run'
      }, [input, el('span', {
        class: 'report-run-label'
      }, [el('strong', {
        text: id
      }), el('span', {
        text: `${fmt((r.program_id ?? r['task.id']))} · ${fmt(r.status)} · ${when(r.started_at)}`
      })])]))
    }
    if (!runs.length) root.append(empty('No retained runs', 'The run ledger returned no records.'));
    $('generate-report').disabled = selectedReports.size === 0
  }
  $('select-visible').addEventListener('click', () => {
    const visible = matchRuns().map(r => String(r.id ?? r.run_id ?? 'unknown')),
      combined = new Set([...selectedReports, ...visible]);
    if (combined.size > 20) {
      flash('Select visible would exceed the 20 run report limit. Narrow the search or deselect runs first.', 'warning');
      return
    }
    visible.forEach(id => selectedReports.add(id));
    renderReportChoices();
    renderRuns()
  });
  $('generate-report').addEventListener('click', async () => {
    const btn = $('generate-report'),
      out = $('report-result'),
      ids = [...selectedReports],
      reportEpoch = epoch,
      reportMode = mode;
    if (!ids.length) return;
    btn.disabled = true;
    btn.textContent = 'Generating…';
    out.className = 'report-result';
    if (reportMode === 'demo') {
      const sample = {
        sample: true,
        label: 'SAMPLE DATA — DEMO MODE',
        generated_at: new Date().toISOString(),
        runs: runs.filter(r => ids.includes(String(r.id ?? r.run_id)))
      };
      let content, filename, mime;
      const format = $('report-format').value;
      if (format === 'json') {
        content = JSON.stringify(sample, null, 2);
        filename = 'sample-telemetry-report.json';
        mime = 'application/json'
      } else if (format === 'markdown') {
        content = `# SAMPLE DATA — Telemetry report\n\nGenerated: ${sample.generated_at}\n\n${sample.runs.map(r=>`## ${fmt(r.id)}\n\n- Program: ${fmt((r.program_id ?? r['task.id']))}\n- Status: ${fmt(r.status)}\n- Summary: ${fmt(r.summary)}`).join('\n\n')}`;
        filename = 'sample-telemetry-report.md';
        mime = 'text/markdown'
      } else {
        content = `sample,run_id,program_id,status,started_at\n${sample.runs.map(r=>`true,"${csv(r.id)}","${csv((r.program_id ?? r['task.id']))}","${csv(r.status)}","${csv(r.started_at)}"`).join('\n')}`;
        filename = 'sample-telemetry-report.csv';
        mime = 'text/csv'
      }
      if (reportEpoch === epoch && mode === reportMode) download(filename, content, mime);
      out.textContent = `Sample report downloaded for ${ids.length} run${ids.length===1?'':'s'}. This is illustrative data.`;
      out.classList.add('success');
      btn.disabled = false;
      btn.textContent = 'Generate report';
      return
    }
    try {
      const response = await api('/console/reports', {
        method: 'POST',
        body: JSON.stringify({
          run_ids: ids,
          format: $('report-format').value
        })
      });
      if (reportEpoch !== epoch || mode !== reportMode) return;
      const blob = await response.blob();
      if (reportEpoch !== epoch || mode !== reportMode) return;
      const filename = dispositionName(response.headers.get('content-disposition')) || `telemetry-report.${$('report-format').value}`;
      download(filename, blob, blob.type || 'application/octet-stream');
      out.textContent = `Report downloaded · ${ids.length} runs · generated ${new Date().toLocaleTimeString()}.`;
      out.classList.add('success')
    } catch (err) {
      if (reportEpoch === epoch && mode === reportMode) {
        out.textContent = `Report failed: ${err.message}`;
        out.classList.add('error')
      }
    } finally {
      if (reportEpoch === epoch) {
        btn.disabled = selectedReports.size === 0;
        btn.textContent = 'Generate report'
      }
    }
  });

  function csv(v) {
    return String(v ?? '').replaceAll('"', '""')
  }

  function dispositionName(v) {
    const m = v && v.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
    return m ? decodeURIComponent(m[1].replaceAll('"', '')) : null
  }
  $('refresh-button').addEventListener('click', refreshAll);

  function init() {
    setMode('offline');
    renderConfig();
    renderAll();
    const initial = location.hash.slice(1);
    if (initial) go(initial);
  }
  init();
})();
