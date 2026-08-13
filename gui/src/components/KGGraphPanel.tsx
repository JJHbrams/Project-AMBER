import { useRef, useEffect, useCallback, useState } from "react";
import { Network } from "vis-network";
import { DataSet } from "vis-data";
import type { Options, Node, Edge } from "vis-network";
import { TauriBridge } from "../ipc/tauri_bridge";
import type { KGNodeData, KGEdgeData, EpisodeNodeData, EpisodeEdgeData } from "../types/kg";

const POLL_INTERVAL = 5000;
const DEFAULT_REPULSION = -60;
const DEFAULT_SPRING_LEN = 140;
const DEFAULT_SPRING_CONST = 0.06;
const DEFAULT_GRAVITY = 0.005;
const DEFAULT_DAMPING = 0.4;

const NODE_TYPE_COLORS: Record<string, string> = {
  concept: "#7c4dff",
  project: "#2563eb",
  research: "#0891b2",
  reference: "#16a34a",
  moc: "#ff9800",
  person: "#e91e63",
  tool: "#ffc107",
  fleeting: "#555",
};

function makeVisNode(n: KGNodeData, highlight: string[], sizeScale: number): Node {
  const color = NODE_TYPE_COLORS[n.type] ?? "#888";
  const isHighlighted = highlight.includes(n.id);
  const baseSize = n.type === "project" || n.type === "moc" ? 18 : 14;
  return {
    id: n.id,
    label: n.title.length > 24 ? `${n.title.slice(0, 22)}…` : n.title,
    title: `[${n.type}] ${n.title}${n.summary ? `\n${n.summary.slice(0, 120)}` : ""}`,
    color: isHighlighted
      ? { background: "#ff9800", border: "#ffcc02", highlight: { background: "#ffcc02", border: "#fff" } }
      : { background: `${color}33`, border: color, highlight: { background: `${color}66`, border: color } },
    font: { color: "#e8e8e8", size: 12 },
    borderWidth: isHighlighted ? 2 : 1,
    size: Math.max(8, Math.round(baseSize * sizeScale)),
    shape: n.type === "moc" ? "diamond" : "dot",
  };
}

function makeVisEdge(e: KGEdgeData, id: string): Edge {
  return {
    id,
    from: e.from,
    to: e.to,
    label: e.rel_type,
    color: { color: "#333", highlight: "#666" },
    font: { color: "#555", size: 10, align: "middle" },
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    width: 1,
  };
}

function makeEpisodeNode(n: EpisodeNodeData): Node {
  const label = n.content.slice(0, 35) + (n.content.length > 35 ? "…" : "");
  return {
    id: `ep:${n.id}`,
    label,
    title: `[memory]\n${n.content.slice(0, 200)}`,
    color: { background: "#0d1b2a", border: "#3f51b5", highlight: { background: "#1a2744", border: "#7986cb" } },
    font: { color: "#7986cb", size: 10 },
    shape: "triangleDown",
    size: 9,
    borderWidth: 1,
  };
}

function makeSemanticEdge(e: EpisodeEdgeData, id: string): Edge {
  const score = e.score ?? e.weight ?? 0;
  const provenance = [
    e.method || e.rel_type,
    `score: ${score.toFixed(4)}`,
    e.keywords ? `keywords: ${e.keywords}` : "",
    e.model ? `model: ${e.model}` : "",
    e.version ? `version: ${e.version}` : "",
    e.created_at ? `created: ${e.created_at}` : "",
  ].filter(Boolean).join("\n");
  return {
    id,
    from: `ep:${e.from}`,
    to: e.to,
    label: "",
    title: provenance,
    color: { color: "#1a2040", highlight: "#3f51b5" },
    dashes: true,
    width: Math.min(4, 0.5 + Math.max(0, score)),
    arrows: { to: { enabled: true, scaleFactor: 0.3 } },
  };
}

interface KGGraphPanelProps {
  highlight?: string[];
  onNodeCount?: (count: number) => void;
}

export function KGGraphPanel({ highlight = [], onNodeCount }: KGGraphPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const networkRef = useRef<Network | null>(null);
  const nodesDS = useRef(new DataSet<Node>());
  const edgesDS = useRef(new DataSet<Edge>());
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refreshInFlightRef = useRef(false);
  const refreshQueuedRef = useRef(false);
  const shouldFitRef = useRef(true);

  const highlightRef = useRef(highlight);
  highlightRef.current = highlight;
  const onNodeCountRef = useRef(onNodeCount);
  onNodeCountRef.current = onNodeCount;

  const [networkError, setNetworkError] = useState<string | null>(null);
  const [showControls, setShowControls] = useState(false);
  const [showMem, setShowMem] = useState(true);
  const [showSemantic, setShowSemantic] = useState(true);
  const [maxMemNodes, setMaxMemNodes] = useState(200);
  const [nodeSizeScale, setNodeSizeScale] = useState(1.0);
  const [physicsEnabled, setPhysicsEnabled] = useState(true);
  const [repulsion, setRepulsion] = useState(DEFAULT_REPULSION);
  const [springLen, setSpringLen] = useState(DEFAULT_SPRING_LEN);
  const [springConst, setSpringConst] = useState(DEFAULT_SPRING_CONST);
  const [gravity, setGravity] = useState(DEFAULT_GRAVITY);
  const [damping, setDamping] = useState(DEFAULT_DAMPING);

  const showMemRef = useRef(showMem);
  showMemRef.current = showMem;
  const showSemanticRef = useRef(showSemantic);
  showSemanticRef.current = showSemantic;
  const maxMemNodesRef = useRef(maxMemNodes);
  maxMemNodesRef.current = maxMemNodes;
  const nodeSizeScaleRef = useRef(nodeSizeScale);
  nodeSizeScaleRef.current = nodeSizeScale;
  const physicsEnabledRef = useRef(physicsEnabled);
  physicsEnabledRef.current = physicsEnabled;
  const repulsionRef = useRef(repulsion);
  repulsionRef.current = repulsion;
  const springLenRef = useRef(springLen);
  springLenRef.current = springLen;
  const springConstRef = useRef(springConst);
  springConstRef.current = springConst;
  const gravityRef = useRef(gravity);
  gravityRef.current = gravity;
  const dampingRef = useRef(damping);
  dampingRef.current = damping;

  const fitGraph = useCallback((animated = true) => {
    requestAnimationFrame(() => {
      if (!networkRef.current) return;
      networkRef.current.fit(animated
        ? { animation: { duration: 220, easingFunction: "easeInOutQuad" } }
        : { animation: false });
    });
  }, []);

  const applyPhysics = useCallback(() => {
    if (!networkRef.current) return;
    networkRef.current.setOptions({
      physics: {
        enabled: physicsEnabledRef.current,
        solver: "forceAtlas2Based",
        forceAtlas2Based: {
          gravitationalConstant: repulsionRef.current,
          centralGravity: gravityRef.current,
          springLength: springLenRef.current,
          springConstant: springConstRef.current,
          damping: dampingRef.current,
        },
        stabilization: { enabled: true, iterations: 200, fit: false },
      },
    });
    if (physicsEnabledRef.current) {
      networkRef.current.startSimulation();
    } else {
      networkRef.current.stopSimulation();
    }
  }, []);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) {
      refreshQueuedRef.current = true;
      return;
    }
    refreshInFlightRef.current = true;
    try {
      do {
        refreshQueuedRef.current = false;
        const data = await TauriBridge.fetchKgGraph();
        if (!data.enabled) {
          onNodeCountRef.current?.(0);
          continue;
        }

        const sizeScale = nodeSizeScaleRef.current;

        const incomingKgNodeIds = new Set(data.kg_nodes.map((n) => n.id));
        const existingKgNodeIds = new Set(nodesDS.current.getIds().map(String).filter((id) => !id.startsWith("ep:")));
        const toAddKg: Node[] = [];
        const toUpdateKg: Node[] = [];
        for (const n of data.kg_nodes) {
          const vis = makeVisNode(n, highlightRef.current, sizeScale);
          if (existingKgNodeIds.has(n.id)) {
            toUpdateKg.push(vis);
          } else {
            toAddKg.push(vis);
          }
        }
        const toRemoveKg = [...existingKgNodeIds].filter((id) => !incomingKgNodeIds.has(id));
        if (toAddKg.length) nodesDS.current.add(toAddKg);
        if (toUpdateKg.length) nodesDS.current.update(toUpdateKg);
        if (toRemoveKg.length) nodesDS.current.remove(toRemoveKg);

        const edgeIdOf = (e: KGEdgeData) => `${e.from}__${e.to}__${e.rel_type}`;
        const incomingKgEdgeIds = new Set(data.kg_edges.map(edgeIdOf));
        const existingKgEdgeIds = new Set(edgesDS.current.getIds().map(String).filter((id) => !id.startsWith("sem:")));
        const toAddKgEdges: Edge[] = [];
        const toUpdateKgEdges: Edge[] = [];
        for (const e of data.kg_edges) {
          const id = edgeIdOf(e);
          const vis = makeVisEdge(e, id);
          if (existingKgEdgeIds.has(id)) {
            toUpdateKgEdges.push(vis);
          } else {
            toAddKgEdges.push(vis);
          }
        }
        const toRemoveKgEdges = [...existingKgEdgeIds].filter((id) => !incomingKgEdgeIds.has(id));
        if (toAddKgEdges.length) edgesDS.current.add(toAddKgEdges);
        if (toUpdateKgEdges.length) edgesDS.current.update(toUpdateKgEdges);
        if (toRemoveKgEdges.length) edgesDS.current.remove(toRemoveKgEdges);

        let visibleEpNodeIds = new Set<string>();
        if (showMemRef.current) {
          const visibleEpNodes = data.ep_nodes.slice(0, maxMemNodesRef.current);
          visibleEpNodeIds = new Set(visibleEpNodes.map((n) => `ep:${n.id}`));
          const existingEpIds = new Set(nodesDS.current.getIds().map(String).filter((id) => id.startsWith("ep:")));
          const toAddEp: Node[] = [];
          const toUpdateEp: Node[] = [];
          for (const n of visibleEpNodes) {
            const vis = makeEpisodeNode(n);
            const id = `ep:${n.id}`;
            if (existingEpIds.has(id)) {
              toUpdateEp.push(vis);
            } else {
              toAddEp.push(vis);
            }
          }
          const toRemoveEp = [...existingEpIds].filter((id) => !visibleEpNodeIds.has(id));
          if (toAddEp.length) nodesDS.current.add(toAddEp);
          if (toUpdateEp.length) nodesDS.current.update(toUpdateEp);
          if (toRemoveEp.length) nodesDS.current.remove(toRemoveEp);
        } else {
          const toRemoveEp = nodesDS.current.getIds().map(String).filter((id) => id.startsWith("ep:"));
          if (toRemoveEp.length) nodesDS.current.remove(toRemoveEp);
        }

        if (showSemanticRef.current && showMemRef.current) {
          const semId = (e: { from: string; to: string; rel_type: string }) =>
            `sem:ep:${e.from}__${e.to}__${e.rel_type}`;
          const visibleSemEdges = data.ep_edges.filter((e) => visibleEpNodeIds.has(`ep:${e.from}`));
          const incomingSemIds = new Set(visibleSemEdges.map(semId));
          const existingSemIds = new Set(edgesDS.current.getIds().map(String).filter((id) => id.startsWith("sem:")));
          const toAddSem: Edge[] = [];
          const toUpdateSem: Edge[] = [];
          for (const e of visibleSemEdges) {
            const id = semId(e);
            const vis = makeSemanticEdge(e, id);
            if (existingSemIds.has(id)) {
              toUpdateSem.push(vis);
            } else {
              toAddSem.push(vis);
            }
          }
          const toRemoveSem = [...existingSemIds].filter((id) => !incomingSemIds.has(id));
          if (toAddSem.length) edgesDS.current.add(toAddSem);
          if (toUpdateSem.length) edgesDS.current.update(toUpdateSem);
          if (toRemoveSem.length) edgesDS.current.remove(toRemoveSem);
        } else {
          const toRemoveSem = edgesDS.current.getIds().map(String).filter((id) => id.startsWith("sem:"));
          if (toRemoveSem.length) edgesDS.current.remove(toRemoveSem);
        }

        setNetworkError(null);
        onNodeCountRef.current?.(data.kg_nodes.length);

        if (shouldFitRef.current && data.kg_nodes.length > 0) {
          shouldFitRef.current = false;
          fitGraph(true);
        }
      } while (refreshQueuedRef.current);
    } catch (err) {
      setNetworkError(err instanceof Error ? err.message : String(err));
    } finally {
      refreshInFlightRef.current = false;
    }
  }, [fitGraph]);

  useEffect(() => {
    if (!containerRef.current) return;
    let networkInst: Network | null = null;
    let resizeRo: ResizeObserver | null = null;

    const initNetwork = (container: HTMLDivElement) => {
      if (networkInst) return;
      try {
        const options: Options = {
          physics: {
            enabled: physicsEnabledRef.current,
            solver: "forceAtlas2Based",
            forceAtlas2Based: {
              gravitationalConstant: repulsionRef.current,
              centralGravity: gravityRef.current,
              springLength: springLenRef.current,
              springConstant: springConstRef.current,
              damping: dampingRef.current,
            },
            stabilization: { enabled: true, iterations: 200, fit: true },
          },
          interaction: { hover: true, tooltipDelay: 120, navigationButtons: false, keyboard: false },
          layout: { improvedLayout: true },
          nodes: { shape: "dot", size: 14 },
          edges: { smooth: { enabled: true, type: "continuous", roundness: 0.3 } },
        };
        networkInst = new Network(container, { nodes: nodesDS.current, edges: edgesDS.current }, options);
        networkRef.current = networkInst;

        networkInst.on("stabilizationIterationsDone", () => {
          if (!networkRef.current || !physicsEnabledRef.current) return;
          setPhysicsEnabled(false);
        });

        resizeRo = new ResizeObserver(() => {
          networkRef.current?.redraw();
        });
        resizeRo.observe(container);

        shouldFitRef.current = true;
        void refresh();
        timerRef.current = setInterval(() => { void refresh(); }, POLL_INTERVAL);
      } catch (err) {
        console.error("[KGGraphPanel] vis-network initialization failed:", err);
        setNetworkError(err instanceof Error ? err.message : String(err));
      }
    };

    const initRo = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        if (width > 0 && height > 0 && !networkInst) {
          initNetwork(containerRef.current!);
          initRo.disconnect();
        }
      }
    });
    initRo.observe(containerRef.current);

    if (containerRef.current.clientWidth > 0 && containerRef.current.clientHeight > 0) {
      initNetwork(containerRef.current);
      initRo.disconnect();
    }

    return () => {
      initRo.disconnect();
      resizeRo?.disconnect();
      if (timerRef.current) clearInterval(timerRef.current);
      networkInst?.destroy();
      networkRef.current = null;
    };
  }, [refresh]);

  useEffect(() => {
    applyPhysics();
  }, [physicsEnabled, repulsion, springLen, springConst, gravity, damping, applyPhysics]);

  useEffect(() => {
    shouldFitRef.current = true;
    void refresh();
  }, [showMem, showSemantic, maxMemNodes, refresh]);

  useEffect(() => {
    void refresh();
  }, [nodeSizeScale, highlight, refresh]);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div className="kg-graph-panel" ref={containerRef} style={{ width: "100%", height: "100%" }} />
      {networkError && (
        <div className="kg-graph-panel__placeholder" style={{ zIndex: 5 }}>
          <span style={{ color: "var(--status-error)", fontSize: 12 }}>⚠ Graph init failed</span>
          <span style={{ color: "var(--text-secondary)", fontSize: 11, maxWidth: 260, textAlign: "center" }}>
            {networkError}
          </span>
        </div>
      )}
      <button
        className="kg-controls-btn"
        onClick={() => setShowControls((v) => !v)}
        title="Graph controls"
      >
        ⚙
      </button>
      {showControls && (
        <div className="kg-controls-panel">
          <div className="kg-controls-title">Graph Controls</div>
          <label className="kg-ctrl-toggle">
            <input type="checkbox" checked={physicsEnabled} onChange={(e) => setPhysicsEnabled(e.target.checked)} />
            <span>Physics</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Repulsion</span>
            <input type="range" min={-300} max={-10} step={10} value={repulsion}
              onChange={(e) => setRepulsion(+e.target.value)} />
            <span>{repulsion}</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Spring Len</span>
            <input type="range" min={50} max={400} step={10} value={springLen}
              onChange={(e) => setSpringLen(+e.target.value)} />
            <span>{springLen}</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Spring K</span>
            <input type="range" min={0.01} max={0.3} step={0.01} value={springConst}
              onChange={(e) => setSpringConst(+e.target.value)} />
            <span>{springConst.toFixed(2)}</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Gravity</span>
            <input type="range" min={0} max={0.05} step={0.001} value={gravity}
              onChange={(e) => setGravity(+e.target.value)} />
            <span>{gravity.toFixed(3)}</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Damping</span>
            <input type="range" min={0.1} max={1.0} step={0.05} value={damping}
              onChange={(e) => setDamping(+e.target.value)} />
            <span>{damping.toFixed(2)}</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Node Size</span>
            <input type="range" min={0.5} max={3.0} step={0.1} value={nodeSizeScale}
              onChange={(e) => setNodeSizeScale(+e.target.value)} />
            <span>{nodeSizeScale.toFixed(1)}x</span>
          </label>
          <div className="kg-ctrl-sep" />
          <label className="kg-ctrl-toggle">
            <input type="checkbox" checked={showMem} onChange={(e) => setShowMem(e.target.checked)} />
            <span>Memories (ep_nodes)</span>
          </label>
          <label className="kg-ctrl-toggle">
            <input type="checkbox" checked={showSemantic} onChange={(e) => setShowSemantic(e.target.checked)} />
            <span>Semantic edges</span>
          </label>
          <label className="kg-ctrl-row">
            <span>Mem Limit</span>
            <input type="range" min={20} max={1000} step={20} value={maxMemNodes}
              onChange={(e) => setMaxMemNodes(+e.target.value)} />
            <span>{maxMemNodes}</span>
          </label>
          <div className="kg-ctrl-actions">
            <button
              type="button"
              onClick={() => {
                shouldFitRef.current = true;
                void refresh();
              }}
            >
              Recenter
            </button>
            <button
              type="button"
              onClick={() => {
                setPhysicsEnabled(true);
                setRepulsion(DEFAULT_REPULSION);
                setSpringLen(DEFAULT_SPRING_LEN);
                setSpringConst(DEFAULT_SPRING_CONST);
                setGravity(DEFAULT_GRAVITY);
                setDamping(DEFAULT_DAMPING);
                setNodeSizeScale(1.0);
                shouldFitRef.current = true;
              }}
            >
              Reset
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
