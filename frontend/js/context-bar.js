export function updateContextBar(usageInfo, serverInfo) {
  const fill = document.getElementById('context-fill');
  const info = document.getElementById('context-info');

  const total    = serverInfo?.model_len || usageInfo?.max_model_len || 0;
  const used     = usageInfo?.prompt_tokens || 0;
  const pct      = usageInfo?.context_pct ?? null;
  const srvLabel = serverInfo?.name ? ` [${serverInfo.name}]` : '';
  const modelTip = serverInfo?.model ? `\n모델: ${serverInfo.model}` : '';

  if (pct == null || (!pct && !used && !total)) {
    fill.style.width = '0%';
    info.textContent = '컨텍스트 정보 없음';
    info.title = '';
    return;
  }

  if (!used && total) {
    fill.style.width = '0%';
    info.textContent = `한계 ${total.toLocaleString()} tokens${srvLabel}`;
    info.title = `한계: ${total.toLocaleString()} tokens${modelTip}\n(서버가 입력 토큰 수를 반환하지 않음)`;
    return;
  }

  const warn = pct >= 0.8 ? ' ⚠️' : '';
  fill.style.width = `${Math.min(pct * 100, 100)}%`;
  if (pct < 0.6)      fill.style.background = '#22c55e';
  else if (pct < 0.8) fill.style.background = '#eab308';
  else                fill.style.background = '#ef4444';

  if (total) {
    const avail = total - used;
    info.textContent = `${(pct*100).toFixed(1)}%${warn} | ${used.toLocaleString()} / ${total.toLocaleString()} | 가용 ${avail.toLocaleString()}${srvLabel}`;
    info.title = `입력: ${used.toLocaleString()} tokens\n한계: ${total.toLocaleString()} tokens\n가용: ${avail.toLocaleString()} tokens${modelTip}`;
  } else {
    info.textContent = `${(pct*100).toFixed(1)}%${warn}${srvLabel}`;
    info.title = '';
  }
}
