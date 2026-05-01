/**
 * graph_widget.js — engram dashboard vis.js 그래프 런타임
 *
 * 이 파일은 dashboard.py의 build_visjs_html()이 생성하는 HTML에 포함된다.
 * Python이 먼저 다음 전역 변수를 선언한다:
 *   _nodesArr, _edgesArr, _GRAPH_HEIGHT, _baseSizes, _container, _dsN, _dsE, network
 */

// ── 반응형 레이아웃 ───────────────────────────────────────────────────────────
function _getParentViewportHeight() {
    try {
        if (window.parent && window.parent !== window) {
            var h = Number(window.parent.innerHeight || 0);
            if (isFinite(h) && h > 0) return h;
        }
    } catch (_err) {}
    return null;
}
function _targetGraphHeight() {
    var parentH = _getParentViewportHeight();
    if (!parentH) return _GRAPH_HEIGHT;
    var target = Math.floor(parentH * 0.82);
    if (target < 640) target = 640;
    if (target > 1200) target = 1200;
    return target;
}
function _setOwnFrameHeight(h) {
    try {
        if (window.frameElement && window.frameElement.style) {
            window.frameElement.style.height = h + 'px';
            window.frameElement.style.minHeight = h + 'px';
            var p = window.frameElement.parentElement;
            if (p && p.style) {
                p.style.height = h + 'px';
                p.style.minHeight = h + 'px';
            }
            return true;
        }
    } catch (_err) {}
    return false;
}
function _notifyFrameHeight(h) {
    var frameH = Math.max(520, Math.floor(h + 10));
    if (_setOwnFrameHeight(frameH)) return;
    try {
        window.parent.postMessage({
            isStreamlitMessage: true,
            type: "streamlit:setFrameHeight",
            height: frameH
        }, "*");
    } catch (_err) {}
}
function _applyResponsiveLayout() {
    var h = _targetGraphHeight();
    if (_lastAppliedHeight === h) return;
    _lastAppliedHeight = h;
    _container.style.height = h + 'px';
    network.setSize('100%', h + 'px');
    network.redraw();
    if (typeof _pin !== 'undefined' && _pin && _pin.style.display === 'block') {
        var py = parseFloat(_pin.style.top || '0');
        if (isFinite(py) && py + 400 > h) _pin.style.top = Math.max(4, h - 404) + 'px';
    }
    _notifyFrameHeight(h);
}
var _resizeRaf = 0;
var _lastAppliedHeight = 0;
function _onWindowResize() {
    if (_resizeRaf) cancelAnimationFrame(_resizeRaf);
    _resizeRaf = requestAnimationFrame(_applyResponsiveLayout);
}
window.addEventListener('resize', _onWindowResize);
window.addEventListener('orientationchange', _onWindowResize);
setTimeout(_applyResponsiveLayout, 0);
setTimeout(_applyResponsiveLayout, 120);

// ── Physics auto-stop ─────────────────────────────────────────────────────────
var _done = false;
function _stopPhys() {
    if (!_done) {
        _done = true;
        network.setOptions({ physics: { enabled: false } });
        document.getElementById('physOn').checked = false;
    }
}
network.on('stabilizationIterationsDone', _stopPhys);
setTimeout(_stopPhys, 4000);

// ── 클릭 핀 패널 ──────────────────────────────────────────────────────────────
var _pin = document.getElementById('eg-pin');
var _pinnedId = null;
var _visTooltip = null;
function _hideVisTooltip() {
    if (!_visTooltip) _visTooltip = document.querySelector('.vis-tooltip');
    if (_visTooltip) _visTooltip.style.visibility = 'hidden';
}
function _showVisTooltip() {
    if (_visTooltip) _visTooltip.style.visibility = '';
}
network.on('click', function(params) {
    if (params.nodes.length > 0) {
        var nid = params.nodes[0];
        var node = _dsN.get(nid);
        if (node && node._tooltip) {
            _pin.innerHTML = node._tooltip;
            var cpos = network.canvasToDOM(network.getPosition(nid));
            var netH = _container.clientHeight || _GRAPH_HEIGHT;
            var px = cpos.x + 24; var py = Math.max(4, cpos.y - 80);
            if (px + 304 > _container.offsetWidth) px = Math.max(4, cpos.x - 324);
            if (py + 400 > netH) py = Math.max(4, netH - 404);
            _pin.style.left = px + 'px'; _pin.style.top = py + 'px';
            _pin.style.display = 'block';
            _pinnedId = nid;
            _hideVisTooltip();
        }
    } else {
        _pin.style.display = 'none';
        _pinnedId = null;
        _showVisTooltip();
        network.unselectAll();
    }
});
_pin.addEventListener('click', function(e) { e.stopPropagation(); });

// ── 핀 패널 드래그 ────────────────────────────────────────────────────────────
(function() {
    var _dx = 0, _dy = 0, _dragging = false;
    _pin.style.cursor = 'grab';
    _pin.addEventListener('mousedown', function(e) {
        if (e.button !== 0) return;
        _dragging = true; _dx = e.clientX - _pin.offsetLeft; _dy = e.clientY - _pin.offsetTop;
        _pin.style.cursor = 'grabbing'; _pin.style.userSelect = 'none';
        e.stopPropagation(); e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
        if (!_dragging) return;
        _pin.style.left = (e.clientX - _dx) + 'px'; _pin.style.top = (e.clientY - _dy) + 'px';
    });
    document.addEventListener('mouseup', function(e) {
        if (!_dragging) return;
        _dragging = false; _pin.style.cursor = 'grab'; _pin.style.userSelect = '';
    });
})();

// ── 실시간 Physics / 노드 크기 슬라이더 ───────────────────────────────────────
document.getElementById('physOn').addEventListener('change', function() {
    network.setOptions({ physics: { enabled: this.checked } }); _done = !this.checked;
});
function _rc(sid, vid, vfn, dfn, afn) {
    document.getElementById(sid).addEventListener('input', function() {
        var v = vfn(parseFloat(this.value));
        document.getElementById(vid).textContent = dfn ? dfn(v) : v;
        afn(v);
    });
}
_rc('gs', 'gv',
    function(v) { return -v; },
    function(v) { return Math.round(-v); },
    function(v) { network.setOptions({ physics: { forceAtlas2Based: { gravitationalConstant: v } } }); }
);
_rc('sl', 'slv',
    function(v) { return v; },
    function(v) { return Math.round(v); },
    function(v) { network.setOptions({ physics: { forceAtlas2Based: { springLength: v } } }); }
);
_rc('ss', 'ssv',
    function(v) { return v / 10; },
    function(v) { return v.toFixed(1) + 'x'; },
    function(scale) {
        var upd = _nodesArr.map(function(n) { return { id: n.id, size: Math.round(_baseSizes[n.id] * scale) }; });
        _dsN.update(upd);
    }
);
