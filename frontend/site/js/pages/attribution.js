import { api } from '../api.js';
import { ENUMS, fmtMoney, fmtPct } from '../config.js';
import { contributionTable, emptyState, errorBox, errorMessage, esc, factorBars, kv, statStrip, toast } from '../ui.js';

/** 渠道×房型对账条：门店Δ = Σ渠道Δ + 口径差异；渠道Δ = Σ房型Δ + 未分配。 */
function reconStrip(recon) {
  const balanced = recon.balanced && Math.abs(recon.unallocated_total || 0) <= 1;
  return statStrip([
    { label: '门店总变动', value: fmtMoney(recon.store_delta), hint: 'LMDI 同一基线口径' },
    { label: 'Σ 渠道变动', value: fmtMoney(recon.channel_delta_sum), hint: '分渠道中位数基线' },
    { label: '口径差异', value: fmtMoney(recon.difference), hint: '单列不摊派' },
    { label: '未分配差额', value: fmtMoney(recon.unallocated_total), hint: '结构缺失 / 占比异常' },
    { label: '对账状态', value: balanced ? '对平' : '有差额', tone: balanced ? 'ok' : 'warn', hint: balanced ? '各层恒等式成立' : '差额已单列说明' },
  ]);
}

function roomTypeTable(block) {
  const rows = block.room_types || [];
  const parts = [];
  if (block.status !== 'ok') {
    parts.push(`<div class="error-box" data-testid="crt-unallocated">
      <span>${esc(ENUMS.drillStatus[block.status] || block.status)}：${esc((block.unallocated || {}).reason || '')}；
      该渠道变动 ${fmtMoney(block.delta)} 全额计入未分配差额，不向房型分摊。</span>
    </div>`);
  }
  if (rows.length) {
    parts.push(`<div class="table-wrap"><table class="data compact">
      <thead><tr><th>房型</th><th class="right">当期占比</th><th class="right">基线占比</th><th class="right">当期</th><th class="right">基线</th><th class="right">变动</th><th class="right">贡献占比</th></tr></thead>
      <tbody>${rows
        .map(
          (row) => `<tr>
            <td>${esc(row.key)}</td>
            <td class="num">${fmtPct(row.share_current)}</td>
            <td class="num">${fmtPct(row.share_base)}</td>
            <td class="num">${fmtMoney(row.current)}</td>
            <td class="num">${fmtMoney(row.base)}</td>
            <td class="num ${row.delta < 0 ? 'neg' : 'pos'}">${fmtMoney(row.delta)}</td>
            <td class="num">${row.contribution === null || row.contribution === undefined ? '—' : fmtPct(row.contribution)}</td>
          </tr>`,
        )
        .join('')}</tbody></table></div>`);
  }
  if (block.unallocated && block.status === 'ok') {
    parts.push(`<p class="muted small">未分配差额 ${fmtMoney(block.unallocated.delta)}（${esc(block.unallocated.reason)}），单列不摊派。</p>`);
  }
  if (!rows.length && block.status === 'ok') {
    parts.push(emptyState({ title: '该渠道没有房型结构数据', hint: '当期与基线占比均为空。' }));
  }
  return parts.join('');
}

/** 渠道×房型下钻面板：渠道选择器 + 选中渠道的房型展开。 */
function channelRoomTypePanel(detail) {
  const crt = (detail.dimensions || {}).channel_room_type;
  if (!crt || !crt.channels) {
    return `<section class="panel">
      <div class="panel-head"><h2>渠道 × 房型下钻</h2><span class="meta">归因版本 v${esc(detail.version)}</span></div>
      <div class="panel-body">${emptyState({
        title: `该归因按 v${detail.version} 口径生成`,
        hint: 'v1 为固定 52/36/12 分摊，不含渠道×房型下钻；既有结论不可改写，新检测命中的异常将按渠道逐日上报结构生成 v2 下钻。',
      })}</div>
    </section>`;
  }
  const recon = crt.reconciliation || {};
  return `<section class="panel" data-testid="crt-panel">
    <div class="panel-head">
      <h2>渠道 × 房型下钻</h2>
      <span class="meta">归因版本 v${esc(detail.version)} · 按各渠道逐日上报结构 · 报告引用同一版本</span>
    </div>
    <div class="panel-body">
      ${reconStrip(recon)}
      <div class="tabs" id="crt-tabs">
        ${crt.channels
          .map(
            (block, index) => `<button class="tab crt-tab ${index === 0 ? 'active' : ''}" data-channel="${esc(block.channel)}">
              ${esc(block.label)}${block.status !== 'ok' ? ` · ${esc(ENUMS.drillStatus[block.status] || block.status)}` : ''}
            </button>`,
          )
          .join('')}
      </div>
      <div id="crt-body"></div>
      <p class="muted small">渠道Δ = Σ房型Δ + 未分配差额；门店Δ = Σ渠道Δ + 口径差异。结构缺失或占比异常的渠道不向房型分摊，全额单列并说明原因。</p>
    </div>
  </section>`;
}

function bindChannelTabs(view, detail) {
  const crt = (detail.dimensions || {}).channel_room_type;
  const body = view.querySelector('#crt-body');
  if (!crt || !body) return;
  const renderChannel = (channelKey) => {
    const block = (crt.channels || []).find((item) => item.channel === channelKey) || (crt.channels || [])[0];
    body.innerHTML = block ? roomTypeTable(block) : '';
  };
  view.querySelectorAll('.crt-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      view.querySelectorAll('.crt-tab').forEach((node) => node.classList.remove('active'));
      tab.classList.add('active');
      renderChannel(tab.dataset.channel);
    });
  });
  renderChannel((crt.channels[0] || {}).channel);
}

export const attributionPage = {
  async render(view, ctx) {
    view.innerHTML = '<div class="empty"><div class="mark">调取归因…</div></div>';
    let list;
    let detail;
    try {
      list = await api.anomalies.list({ limit: 40 });
      const targetId = ctx.query.anomaly_id || (list.items[0] || {}).id;
      if (!targetId) {
        view.innerHTML = `<section class="panel">${errorBox('还没有可归因的异常，请先发起一次检测。')}</section>`;
        return;
      }
      detail = await api.attributions.forAnomaly(targetId);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => attributionPage.render(view, ctx));
      return;
    }
    const shares = detail.shares;
    const dims = detail.dimensions || {};
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>归因下钻</h2>
          <div class="head-actions">
            <span class="meta">算法 B · ${esc(detail.method)} · 归因 v${esc(detail.version)}</span>
            ${ctx.user.role !== 'viewer' ? '<button class="btn sm ghost" id="export-attr">导出归因 CSV</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="pickers">
            ${list.items
              .slice(0, 12)
              .map(
                (item) => `<a class="pick ${String(item.id) === String(detail.anomaly_id) ? 'active' : ''}" href="#/attribution?anomaly_id=${item.id}" data-testid="pick-${item.id}">
                  <b>${esc(item.store_name)}</b>
                  <span class="mono">${esc(item.metric_date)} · z ${item.robust_z.toFixed(2)} · ${fmtMoney(item.impact_amount)}</span>
                </a>`,
              )
              .join('')}
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>${esc(detail.store_name)} · ${esc(detail.metric_date)}</h2>
          <span class="meta">Δ营收 ${fmtMoney(detail.total_delta)} · 残差 ${detail.residual.toFixed(2)} 元</span>
        </div>
        <div class="panel-body split">
          <div>
            <h3 class="sub-head">量价分解（LMDI 加法形式）</h3>
            ${factorBars({ parts: detail.parts, shares })}
            <p class="muted small">对数平均权重 ${detail.drivers.log_mean_weight ?? '—'}；基线 ${fmtMoney(detail.base_rev)} → 当期 ${fmtMoney(detail.cur_rev)}；三项之和与 Δ 的差额如实记为残差，不摊派。</p>
            ${kv([
              ['基线 ADR', `${Number(detail.drivers.base.adr).toFixed(0)} 元`],
              ['基线 OCC', fmtPct(detail.drivers.base.occ)],
              ['基线可售房量', detail.drivers.base.rooms],
              ['当期 ADR', `${Number(detail.drivers.current.adr).toFixed(0)} 元`],
              ['当期 OCC', fmtPct(detail.drivers.current.occ)],
              ['当期可售房量', detail.drivers.current.rooms],
            ])}
          </div>
          <div>
            <h3 class="sub-head">结论（写入报告与导出）</h3>
            <div class="narrative" data-testid="narrative">${esc(detail.narrative)}</div>
            <h3 class="sub-head">三维贡献</h3>
            <div class="tabs">
              ${['channel', 'store', 'room_type']
                .map(
                  (key, index) =>
                    `<button class="tab ${index === 0 ? 'active' : ''}" data-tab="${key}">${esc((dims[key] || {}).label || key)}</button>`,
                )
                .join('')}
            </div>
            <div id="dim-body">${contributionTable(dims.channel || {})}</div>
          </div>
        </div>
      </section>

      ${channelRoomTypePanel(detail)}

      <section class="panel">
        <div class="panel-head"><h2>同维度明细</h2><span class="meta">contribution = group Δ / total Δ</span></div>
        <div class="panel-body grid-3">
          ${['channel', 'store', 'room_type']
            .map(
              (key) => `<div class="dim-block">
                <h3 class="sub-head">${esc((dims[key] || {}).label || key)}</h3>
                ${contributionTable(dims[key] || {})}
              </div>`,
            )
            .join('')}
        </div>
      </section>`;
    view.querySelectorAll('.tab:not(.crt-tab)').forEach((tab) => {
      tab.addEventListener('click', () => {
        view.querySelectorAll('.tab:not(.crt-tab)').forEach((node) => node.classList.remove('active'));
        tab.classList.add('active');
        const block = dims[tab.dataset.tab];
        const body = view.querySelector('#dim-body');
        if (body) body.innerHTML = contributionTable(block || {});
      });
    });
    bindChannelTabs(view, detail);
    view.querySelector('#export-attr')?.addEventListener('click', async () => {
      try {
        await api.download('/api/exports/attributions.csv', 'attributions.csv');
        toast('已开始下载归因明细');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
  },
};

export default attributionPage;
