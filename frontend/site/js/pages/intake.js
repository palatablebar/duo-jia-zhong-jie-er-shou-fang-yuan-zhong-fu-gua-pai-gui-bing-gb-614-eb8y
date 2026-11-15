import { api } from '../api.js';
import { ENUMS, can, stageTone } from '../config.js';
import { badge, drawer, errorBox, errorMessage, esc, field, fmtTime, table, toast } from '../ui.js';

const state = { agencyCode: '' };

function stageCell(row) {
  return `<span class="badge ${stageTone('complete')}">完整 ${esc(row.complete_count)}</span>
    <span class="badge ${stageTone('incomplete')}">待补 ${esc(row.incomplete_count)}</span>
    <span class="badge ${stageTone('invalid')}">不合规 ${esc(row.invalid_count)}</span>`;
}

function openBatch(view, ctx, item) {
  const { root } = drawer({
    title: `采集批次 ${esc(item.batch_no)}`,
    subtitle: `${esc(item.agency_code)} · ${esc(item.status_label)} · ${fmtTime(item.created_at)}`,
    body: `<dl class="kv">
      <dt>接收条数</dt><dd class="mono">${esc(item.received_count)}</dd>
      <dt>分诊结果</dt><dd>${stageCell(item)}</dd>
      <dt>写入结果</dt><dd class="mono">新增 ${esc(item.created_count)} · 更新 ${esc(item.updated_count)} · 未变化 ${esc(item.unchanged_count)}</dd>
      <dt>重试链</dt><dd class="mono">${esc(item.retry_of || '首次提交')}</dd>
      <dt>提交人</dt><dd class="mono">${esc(item.submitted_by)}</dd>
      <dt>处理时间</dt><dd>${fmtTime(item.processed_at)}</dd>
      <dt>备注</dt><dd>${esc(item.note || '—')}</dd>
      <dt>失败原因</dt><dd>${esc(item.last_error || '—')}</dd>
    </dl>
    <p class="note">同批次号重试按 (中介, 挂牌编号) 自然键 upsert：已存在的不新增，未变化记「未变化」，补正后重新分诊。</p>`,
    footer: can(ctx.user.role, 'intake:write')
      ? '<button class="btn accent" data-retry>整批重试（同批不新增）</button>'
      : '<span class="mono muted">只读角色不能发起重试</span>',
  });
  root.querySelector('[data-retry]')?.addEventListener('click', async () => {
    try {
      const refreshed = await api.post(`/api/intake/batches/${encodeURIComponent(item.batch_no)}/retry`, {});
      toast(`重试完成：完整 ${refreshed.complete_count} / 待补 ${refreshed.incomplete_count} / 不合规 ${refreshed.invalid_count}`);
      root.querySelector('[data-close]')?.click();
      await intakePage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const intakePage = {
  async render(view, ctx) {
    let data;
    let agencies;
    try {
      [data, agencies] = await Promise.all([
        api.get('/api/intake/batches', { agency_code: state.agencyCode, limit: 50 }),
        api.get('/api/agencies', { limit: 50 }),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => intakePage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>采集批次台账</h2>
          <span class="meta">${data.total} 个批次 · 谁被拦、补正后是否重新入列都在这里</span>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="batch-filters">
            ${field({ label: '中介渠道', name: 'agency_code', value: state.agencyCode, options: [{ value: '', label: '全部' }, ...agencies.items.map((item) => ({ value: item.code, label: item.name }))] })}
          </div>
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'batch_no', label: '批次号', className: 'mono' },
            { key: 'agency_code', label: '中介', render: (row) => `<b>${esc(row.agency_code)}</b>` },
            { key: 'status', label: '状态', render: (row) => badge(row.status === 'processed' ? 'ok' : 'warn', row.status_label) },
            { key: 'received_count', label: '接收', className: 'num' },
            {
              key: 'stage',
              label: '分诊（完整/待补/不合规）',
              render: stageCell,
            },
            { key: 'write', label: '新增/更新/未变化', className: 'mono', render: (row) => `${esc(row.created_count)} / ${esc(row.updated_count)} / ${esc(row.unchanged_count)}` },
            { key: 'retry_of', label: '重试链', className: 'mono', render: (row) => esc(row.retry_of || '—') },
            { key: 'created_at', label: '提交时间', render: (row) => fmtTime(row.created_at) },
          ],
          empty: { title: '还没有采集批次', hint: '新接入门店的批次入库后会出现在这里。' },
        })}
      </section>
      <section class="panel">
        <div class="panel-head"><h2>分诊口径</h2></div>
        <div class="panel-body">
          <div class="chips">
            <span class="chip">${badge('ok', ENUMS.ingestStage.complete)} 小区/户型/面积/价格均可解析，可进入房源簇</span>
            <span class="chip">${badge('warn', ENUMS.ingestStage.incomplete)} 价格面议、面积待核实等缺项，补正后可重新处理</span>
            <span class="chip">${badge('danger', ENUMS.ingestStage.invalid)} 小区/户型无法解析或面积越界，退回渠道整改</span>
          </div>
          <p class="note" style="margin-top:10px">采集原文全量留档（raw_payload），补正只写补正台账、不覆盖原文；待补与不合规记录不参与归并，也不会出现在银行清单中。</p>
        </div>
      </section>`;
    view.querySelector('#batch-filters')?.addEventListener('change', () => {
      state.agencyCode = view.querySelector('[name=agency_code]').value;
      intakePage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openBatch(view, ctx, item);
      });
    });
  },
};

export default intakePage;
