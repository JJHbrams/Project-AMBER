export interface KGNodeData {
  id: string;
  title: string;
  type: string;
  tags?: string[];
  summary?: string;
}

export interface KGEdgeData {
  from: string;
  to: string;
  rel_type: string;
  weight?: number;
}

export interface EpisodeNodeData {
  id: string;
  content: string;
  keywords?: string[];
  session_id?: number;
  created_at?: string;
}

export interface GraphData {
  enabled: boolean;
  kg_nodes: KGNodeData[];
  kg_edges: KGEdgeData[];
  ep_nodes: EpisodeNodeData[];
  ep_edges: Array<{ from: string; to: string; rel_type: string }>;
  error?: string;
}

export interface SGStats {
  enabled: boolean;
  kg_nodes: number;
  episode_nodes: number;
  ep_to_kg: number;
  kg_edges: number;
}
