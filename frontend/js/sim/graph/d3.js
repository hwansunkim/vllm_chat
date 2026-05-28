// frontend/js/sim/graph/d3.js
// D3 force-directed graph for the simulation right panel.
// Module-private state — only the explicit exports cross the boundary.

import { sim, EMOTION_COLORS, emotionColor } from '../state.js';

let _d3Sim  = null;
let _d3Data = { nodes: [], links: [], nodeMap: {} };

export function initD3Graph() {
  _d3Data = { nodes: [], links: [], nodeMap: {} };
  const svgEl = document.getElementById('sim-graph-svg');
  const svg   = d3.select(svgEl);
  svg.selectAll('*').remove();

  const W = svgEl.clientWidth  || 280;
  const H = svgEl.clientHeight || 400;

  const defs = svg.append('defs');
  Object.entries(EMOTION_COLORS).forEach(([em, color]) => {
    defs.append('marker')
      .attr('id', `arr-${em}`)
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 24).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', color);
  });
  defs.append('marker').attr('id', 'arr-default')
    .attr('viewBox', '0 -4 8 8').attr('refX', 24).attr('refY', 0)
    .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
    .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', '#a78bfa');

  const g = svg.append('g');

  svg.call(d3.zoom().scaleExtent([0.3, 4])
    .on('zoom', e => g.attr('transform', e.transform)));

  _d3Sim = d3.forceSimulation([])
    .force('link',      d3.forceLink([]).id(d => d.id).distance(110))
    .force('charge',    d3.forceManyBody().strength(-220))
    .force('center',    d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(36));

  _d3Sim.on('tick', () => {
    g.selectAll('.g-link').attr('d', linkPath);
    g.selectAll('.g-link-label')
      .attr('x', d => (d.source.x + d.target.x) / 2)
      .attr('y', d => (d.source.y + d.target.y) / 2 - 5);
    g.selectAll('.g-node').attr('transform', d => `translate(${d.x},${d.y})`);
  });

  svg.datum({ g, W, H });
}

function linkPath(d) {
  const dx = d.target.x - d.source.x;
  const dy = d.target.y - d.source.y;
  const dr = Math.sqrt(dx * dx + dy * dy) * 1.3;
  return `M${d.source.x},${d.source.y}A${dr},${dr} 0 0,1 ${d.target.x},${d.target.y}`;
}

export function addD3Edge(source, target, emotion) {
  if (!_d3Sim) return;
  const svg = d3.select('#sim-graph-svg');
  const gEl = svg.datum()?.g;
  if (!gEl) return;

  [source, target].forEach(name => {
    if (name === 'system' || name === 'all' || _d3Data.nodeMap[name]) return;
    const agent = sim.agents.find(a => a.name === name) || { icon: '🤖', name };
    const node  = { id: name, icon: agent.icon };
    _d3Data.nodes.push(node);
    _d3Data.nodeMap[name] = node;
  });

  if (target !== 'system' && target !== 'all') {
    _d3Data.links.push({ source, target, emotion: emotion || 'neutral' });
  }

  _d3Sim.nodes(_d3Data.nodes);
  _d3Sim.force('link').links(_d3Data.links);

  const linkSel = gEl.selectAll('.g-link').data(_d3Data.links);
  linkSel.enter().append('path').attr('class', 'g-link')
    .merge(linkSel)
    .attr('stroke', d => emotionColor(d.emotion))
    .attr('marker-end', d => `url(#arr-${EMOTION_COLORS[d.emotion] ? d.emotion : 'default'})`);

  const lblSel = gEl.selectAll('.g-link-label').data(_d3Data.links);
  lblSel.enter().append('text').attr('class', 'g-link-label')
    .merge(lblSel).text(d => d.emotion);

  const nodeSel   = gEl.selectAll('.g-node').data(_d3Data.nodes, d => d.id);
  const nodeEnter = nodeSel.enter().append('g').attr('class', 'g-node')
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0); d.fx = null; d.fy = null; })
    );

  nodeEnter.append('circle').attr('r', 22).attr('fill', '#eef2ff');
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', -4)
    .attr('font-size', '17px').text(d => d.icon);
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', 14)
    .attr('font-size', '10px').attr('fill', '#475569').text(d => d.id);

  _d3Sim.alpha(0.4).restart();
}

export function exportGraph() {
  const svgEl = document.getElementById('sim-graph-svg');
  const blob  = new Blob([new XMLSerializer().serializeToString(svgEl)], { type: 'image/svg+xml' });
  const url   = URL.createObjectURL(blob);
  const a     = Object.assign(document.createElement('a'), { href: url, download: 'sim-graph.svg' });
  a.click();
  URL.revokeObjectURL(url);
}

/** Test/debug accessor — returns the live d3 force simulation. */
export function getD3Sim() {
  return _d3Sim;
}
