import { api } from '../api.js';
import { ENUMS, can, riskTone, stageTone, statusTone, tagTone, wan } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, fmtTime, kv, table, toast } from '../ui.js';

const state = { q: '', agencyId: '', status: '', risk: '', stage: '', propertyId: '' };

function pairCell(raw, norm) {
  return `<div class="pair"><span class="raw">${esc(raw)}</span><span class="arrow">→</span><span class="norm">${esc(norm)}</span></div>`;
}

function openCorrection(view, ctx, item) {
  const editable = can(ctx.user.role, 'intake:write');
  const fields = [
    ['community_raw', '小区(原文)'],
    ['layout_raw', '户型(原文)'],
    ['area_raw', '面积(原文)'],
    ['price_raw', '价格(原文)'],
    ['floor_raw', '楼层(原文)'],
    ['orientation_raw', '朝向(原文)'],
    ['title', '标题'],
  ];
  const { root, close } = drawer({
    title: `补正挂牌 ${esc(item.lid)}`,
    subtitle: `当前分诊：${esc(item.ingest_stage_label)} · 缺项：${esc((item.defect_flags || []).join(' / ') || '无')}`,
    body: `<p class="note">补正只更新工作字段并写补正台账，采集原文快照永久留档；补正后仍为待补/不合规会被驳回，原记录不变。</p>
      <form id="correction-form" style="display:grid;gap:10px">
        ${fields
          .map(([name, label]) => field({ label, name, value: item[name] ?? '', placeholder: `修改 ${label}` }))
          .join('')}
        ${field({ label: '补正原因', name: 'reason', value: '', placeholder: '例如：门店核实面积为 89.5㎡' })}
        <div class="form-error" id="correction-error"></div>
        <button class="btn accent" type="submit" ${editable ? '' : 'disabled'}>提交补正并重新分诊</button>
      </form>`,
    footer: '<span class="mono muted">补正成功后需在「归并簇」发起重算，记录才会重新入列</span>',
  });
  root.querySelector('#correction-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.target;
    const changes = {};
    for (const [name] of fields) {
      const value = form[name].value.trim();
      if (value && value !== String(item[name] ?? '')) changes[name] = value;
    }
    if (!Object.keys(changes).length) {
      toast('没有修改任何字段');
      return;
    }
    try {
      const result = await api.post(`/api/listings/${item.id}/corrections`, {
        changes,
        reason: form.reason.value.trim() || null,
      });
      toast(`补正已应用，新分诊：${result.stage_label}`);
      close();
      await listingsPage.render(view, ctx);
    } catch (err) {
      root.querySelector('#correction-error').textContent = errorMessage(err);
    }
  });
}

function openListing(view, ctx, item) {
  const flags = item.parse_flags || [];
  const defects = item.defect_flags || [];
  const editable = can(ctx.user.role, 'intake:write');
  const { root } = drawer({
    title: `${item.community_raw.trim()} · ${item.layout_raw}`,
    subtitle: `${item.lid} · ${item.agency_name} · ${fmtTime(item.captured_at)}`,
    body: `${kv([
      ['采集分诊', `<span class="badge ${stageTone(item.ingest_stage)}">${esc(item.ingest_stage_label)}</span>${
        item.can_merge ? '<span class="chip ok">可入簇</span>' : '<span class="chip">不参与归并</span>'
      }`],
      ['阻断缺项', defects.length ? defects.map((f) => `<span class="chip flag">${esc(f)}</span>`).join('') : '—'],
      ['采集批次', `<span class="mono">${esc(item.ingest_batch || '—')}</span>`],
      ['小区', `${pairCell(item.community_raw, item.norm.community)}`],
      ['户型', `${pairCell(item.layout_raw, item.norm.layout)}`],
      ['面积', `${pairCell(item.area_raw, item.norm.area === null ? '（无法解析）' : `${item.norm.area} ㎡`)}`],
      ['价格', `${pairCell(item.price_raw, item.norm.price ? wan(item.norm.price) : '（无法解析）')}`],
      ['楼层', `${pairCell(item.floor_raw, item.norm.floor ? `${item.norm.floor}/${item.norm.floor_total || '?'} · ${item.norm.floor_band}` : '（仅区间 / 缺失）')}`],
      ['朝向', `${pairCell(item.orientation_raw, item.norm.orientation || '—')}`],
      ['挂牌状态', `<span class="badge ${statusTone(item.status)}">${esc(item.status_label)}</span>${item.withdrawn_at ? ` <span class="mono muted">撤回于 ${fmtTime(item.withdrawn_at)}</span>` : ''}`],
      ['挂牌天数 / 带看', `<span class="mono">${esc(item.listed_days)} 天 · ${esc(item.views)} 次</span>`],
      ['所属簇', item.property_id ? `<span class="mono">#${esc(item.property_id)} ${esc(item.property_community || '')} ${esc(item.property_layout || '')}</span>` : '未入簇'],
      ['风险标签', (item.risk_tags || []).length ? item.risk_tags.map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ') : '—'],
    ])}
    <h3 style="margin:14px 0 8px;font-size:0.95rem">归一标记</h3>
    <div class="chips">${
      flags.length ? flags.map((flag) => `<span class="chip flag">${esc(flag)}</span>`).join('') : '<span class="chip">无（字段规整）</span>'
    }</div>
    <h3 style="margin:14px 0 8px;font-size:0.95rem">标题与切词</h3>
    <p class="note">${esc(item.title)}</p>
    <div class="chips" style="margin-top:8px">${(item.norm.title_tokens || [])
      .slice(0, 18)
      .map((token) => `<span class="chip">${esc(token)}</span>`)
      .join('')}</div>`,
    footer: editable
      ? `<div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn accent" data-correct>补正 / 重新分诊</button>
          ${item.withdrawn_at ? '' : '<button class="btn ghost" data-withdraw>撤回挂牌</button>'}
          ${item.property_id ? '<button class="btn ghost" data-cluster>查看归并簇</button>' : ''}
        </div>`
      : item.property_id
        ? `<button class="btn ghost" data-cluster>查看归并簇 #${esc(item.property_id)}</button>`
        : '<span class="mono muted">该挂牌尚未进入任何簇</span>',
  });
  root.querySelector('[data-cluster]')?.addEventListener('click', () => {
    ctx.navigate(`#/clusters?focus=${item.property_id}`);
  });
  root.querySelector('[data-correct]')?.addEventListener('click', () => openCorrection(view, ctx, item));
  root.querySelector('[data-withdraw]')?.addEventListener('click', async () => {
    try {
      await api.post(`/api/listings/${item.id}/withdraw`, { reason: '人工撤回' });
      toast('挂牌已撤回，下次重算后移出房源簇与银行清单');
      root.querySelector('[data-close]')?.click();
      await listingsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const listingsPage = {
  async render(view, ctx) {
    if (ctx.query.agency_id) state.agencyId = ctx.query.agency_id;
    if (ctx.query.risk) state.risk = ctx.query.risk;
    if (ctx.query.property_id) state.propertyId = ctx.query.property_id;
    let data;
    let agencies;
    try {
      [data, agencies] = await Promise.all([
        api.get('/api/listings', {
          q: state.q,
          agency_id: state.agencyId,
          status: state.status,
          risk: state.risk,
          stage: state.stage,
          property_id: state.propertyId,
          limit: 50,
        }),
        api.get('/api/agencies', { limit: 50 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => listingsPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>挂牌明细 <span class="mono muted">· 原文 vs 归一 · 采集分诊</span></h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${data.total} 条${state.propertyId ? ` · 簇 #${esc(state.propertyId)}` : ''}</span>
            ${
              state.propertyId
                ? '<button class="btn sm ghost" id="clear-cluster">看全部挂牌</button>'
                : ''
            }
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="listing-filters">
            ${field({ label: '搜索', name: 'q', value: state.q, placeholder: '小区 / 编号 / 标题' })}
            ${field({ label: '中介', name: 'agency_id', value: state.agencyId, options: [{ value: '', label: '全部' }, ...agencies.items.map((item) => ({ value: item.id, label: item.name }))] })}
            ${field({ label: '采集分诊', name: 'stage', value: state.stage, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.ingestStage).map(([value, label]) => ({ value, label }))] })}
            ${field({ label: '挂牌状态', name: 'status', value: state.status, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.listingStatus).map(([value, label]) => ({ value, label }))] })}
            ${field({ label: '风险', name: 'risk', value: state.risk, options: [{ value: '', label: '全部' }, { value: 'bait', label: '钓鱼价' }, { value: 'stale', label: '僵尸挂牌' }, { value: 'none', label: '无标签' }] })}
          </div>
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'lid', label: '挂牌编号', className: 'mono' },
            { key: 'agency_name', label: '中介', render: (row) => esc(row.agency_name) },
            { key: 'stage', label: '分诊', render: (row) => `<span class="badge ${stageTone(row.ingest_stage)}">${esc(row.ingest_stage_label)}</span>${row.withdrawn_at ? '<div class="mono muted">已撤回</div>' : ''}` },
            { key: 'community', label: '小区', render: (row) => pairCell(row.community_raw, row.norm.community) },
            { key: 'area', label: '面积', render: (row) => pairCell(row.area_raw, row.norm.area === null ? '—' : `${row.norm.area}㎡`) },
            { key: 'price', label: '价格', render: (row) => pairCell(row.price_raw, row.norm.price ? wan(row.norm.price) : '—') },
            { key: 'floor', label: '楼层', render: (row) => pairCell(row.floor_raw, row.norm.floor ? `${row.norm.floor}/${row.norm.floor_total || '?'} 层` : '仅区间') },
            { key: 'status', label: '状态', render: (row) => `<span class="badge ${statusTone(row.status)}">${esc(row.status_label)}</span>` },
            {
              key: 'risk_tags',
              label: '风险',
              render: (row) =>
                (row.risk_tags || []).length
                  ? row.risk_tags.map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ')
                  : `<span class="muted">${row.property_risk_level ? esc(ENUMS.riskLevel[row.property_risk_level] || '') : '—'}</span>`,
            },
          ],
          empty: { title: '没有匹配的挂牌', hint: '换个中介、分诊或风险筛选再看。' },
        })}
      </section>`;

    const filters = view.querySelector('#listing-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.agencyId = filters.querySelector('[name=agency_id]').value;
      state.status = filters.querySelector('[name=status]').value;
      state.stage = filters.querySelector('[name=stage]').value;
      state.risk = filters.querySelector('[name=risk]').value;
      state.propertyId = '';
      listingsPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        state.propertyId = '';
        listingsPage.render(view, ctx);
      }
    });
    view.querySelector('#clear-cluster')?.addEventListener('click', () => {
      state.propertyId = '';
      listingsPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openListing(view, ctx, item);
      });
    });
  },
};

export default listingsPage;
